import { realpath, stat } from "node:fs/promises";
import { join, resolve } from "node:path";
import { z } from "zod";
import { reasoningEfforts, type AgentClient } from "../harness/openai";
import { createClient, defaultModel, providerStatuses, requireProvider } from "../providers";
import { initializeExecution, newReport, scan, ScanControl } from "../harness/scanner";
import { Store, projectStores } from "../harness/store";
import { stateFor, type Report } from "../harness/types";
import { settingsSchema, settingsPatchSchema, startScanSchema, triagePatchSchema, type FindingRecord, type Settings, type SettingsView, type StartScanInput, type TriagePatch } from "../shared/contracts";
import { readCredentials, effectiveCredentials, saveCredential, removeCredential as deleteSavedCredential, type ApiCredentials, type CredentialProvider } from "./credentials";
import { atomicJson, dataHome, isMissing, privateDirectory, processLock, readRegular, regularDirectory } from "./files";

export interface WorkspaceOptions {
  home?: string;
  clientFactory?: (settings: Settings, credentials: ApiCredentials) => AgentClient;
}
interface ActiveScan { report: Report; abort: AbortController; control: ScanControl; done: Promise<Report> }

/** Shared local scanner service. Reports persist independently of the dashboard process. */
export class LocalWorkspace {
  readonly home: string;
  private active = new Map<string, ActiveScan>();
  private readonly clientFactory: (settings: Settings, credentials: ApiCredentials) => AgentClient;
  private readonly checkProvider: boolean;
  private operations: Promise<unknown> = Promise.resolve();
  private closing = false;

  get activeScanCount(): number { return this.active.size; }

  constructor(options: WorkspaceOptions = {}) {
    this.home = dataHome(options.home);
    this.clientFactory = options.clientFactory ?? createClient;
    this.checkProvider = !options.clientFactory;
  }
  private exclusive<T>(operation: () => Promise<T>): Promise<T> {
    const next = this.operations.then(operation, operation);
    this.operations = next.catch(() => {});
    return next;
  }
  async getConfiguration(): Promise<Settings> {
    let persisted: unknown = {};
    try {
      await regularDirectory(this.home);
      const text = await readRegular(join(this.home, "settings.json"), 64 * 1024);
      try { persisted = JSON.parse(text); }
      catch { throw new Error("Saved OpenHack settings are invalid. Restore a valid settings.json file in the data directory."); }
    }
    catch (error) { if (!isMissing(error)) throw error; }
    const reasoning = reasoningEfforts.find(value => value === process.env.OPENAI_REASONING_EFFORT);
    const parsed = settingsPatchSchema.safeParse(persisted);
    if (!parsed.success) throw new Error("Saved OpenHack settings are invalid. Restore a valid settings.json file in the data directory.");
    const saved = parsed.data;
    const provider = saved.provider ?? "openai";
    const defaults: Settings = {provider, model: defaultModel(provider), workers: 4, steps: 20, context: "", port: 4900, reasoning: provider === "openai" ? reasoning : undefined};
    const merged = {...defaults, ...saved};
    const settings = settingsSchema.parse({...merged, reasoning: merged.reasoning ?? undefined});
    return settings;
  }
  async getSettings(): Promise<SettingsView> { return this.settingsView(await this.getConfiguration()); }
  async updateSettings(patch: z.infer<typeof settingsPatchSchema>): Promise<SettingsView> {
    const valid = settingsPatchSchema.parse(patch);
    await privateDirectory(this.home);
    const release = await processLock(join(this.home, "settings.lock"));
    try {
      const current = await this.getConfiguration();
      const switched = valid.provider !== undefined && valid.provider !== current.provider;
      const merged = {...current, ...(switched ? {model: defaultModel(valid.provider!), reasoning: undefined} : {}), ...valid};
      const settings = settingsSchema.parse({...merged, reasoning: merged.reasoning ?? undefined});
      await atomicJson(join(this.home, "settings.json"), {...settings, reasoning: settings.reasoning ?? null}, { durable: true });
      return this.settingsView(settings);
    } finally { await release(); }
  }
  private async settingsView(settings: Settings): Promise<SettingsView> {
    const credentials = await readCredentials(this.home);
    const effective = effectiveCredentials(credentials);
    return {...settings, home: this.home, apiKeyConfigured: settings.provider === "anthropic" ? !!effective.anthropic : settings.provider === "openai" ? !!effective.openai : false, providers: await providerStatuses(false, credentials)};
  }
  async configureCredential(provider: CredentialProvider, key: string): Promise<SettingsView> {
    await saveCredential(this.home, provider, key);
    return this.getSettings();
  }
  async removeCredential(provider: CredentialProvider): Promise<SettingsView> {
    await deleteSavedCredential(this.home, provider);
    return this.getSettings();
  }
  async refreshProviders() {
    return providerStatuses(true, await readCredentials(this.home));
  }
  private async visible(report: Report, store: Store): Promise<Report> {
    const local = this.active.get(report.id);
    if (local) return structuredClone(local.report);
    if (report.state === "running" && !(await store.isRunning(report.id))) {
      return {...report, state: "cancelled", phase: "Scan interrupted", error: "The scan process stopped. Resume to retry unfinished work."};
    }
    return report;
  }
  async listScans(root?: string): Promise<Report[]> {
    const stores = root ? [new Store(await realpath(root), this.home)] : await projectStores(this.home);
    const groups = await Promise.all(stores.map(async store => Promise.all((await store.history()).map(report => this.visible(report, store)))));
    return groups.flat().sort((a, b) => b.startedAt.localeCompare(a.startedAt));
  }
  async getScan(id: string): Promise<Report> {
    const active = this.active.get(id);
    if (active) return structuredClone(active.report);
    const report = (await this.listScans()).find(scan => scan.id === id);
    if (!report) throw new Error(`Scan ${id} not found.`);
    return report;
  }
  async listFindings(root?: string): Promise<FindingRecord[]> {
    const latest = new Map<string, Report>();
    const reports = await this.listScans(root);
    // Keep the previous findings visible while a new scan has not produced a result.
    for (const report of reports) if (!latest.has(report.root) && (report.state === "complete" || report.findings.length > 0)) latest.set(report.root, report);
    for (const report of reports) if (!latest.has(report.root)) latest.set(report.root, report);
    const groups = await Promise.all([...latest.values()].map(async report => {
      const triage = await new Store(report.root, this.home).triage();
      return report.findings.map(finding => ({...finding, ...stateFor(finding.id, triage), key: `${report.id}:${finding.id}`, scanId: report.id, project: report.project, root: report.root}));
    }));
    return groups.flat();
  }
  async listScanFindings(scanId: string): Promise<FindingRecord[]> {
    const report = await this.getScan(scanId);
    const triage = await new Store(report.root, this.home).triage();
    return report.findings.map(finding => ({...finding, ...stateFor(finding.id, triage), key: `${report.id}:${finding.id}`, scanId: report.id, project: report.project, root: report.root}));
  }
  async getFinding(scanId: string, findingId: string): Promise<FindingRecord> {
    const report = await this.getScan(scanId), finding = report.findings.find(item => item.id === findingId);
    if (!finding) throw new Error(`Finding ${findingId} not found.`);
    const triage = await new Store(report.root, this.home).triage();
    return {...finding, ...stateFor(findingId, triage), key: `${scanId}:${findingId}`, scanId, project: report.project, root: report.root};
  }
  async updateFinding(scanId: string, findingId: string, patch: TriagePatch): Promise<FindingRecord> {
    const valid = triagePatchSchema.parse(patch), report = await this.getScan(scanId);
    const finding = report.findings.find(item => item.id === findingId);
    if (!finding) throw new Error(`Finding ${findingId} not found.`);
    const triage = await new Store(report.root, this.home).update([findingId], valid);
    return {...finding, ...stateFor(findingId, triage), key: `${scanId}:${findingId}`, scanId, project: report.project, root: report.root};
  }
  async startScan(input: StartScanInput): Promise<Report> {
    const valid = startScanSchema.parse(input);
    return this.exclusive(async () => {
      if (this.closing) throw new Error("The scanner is shutting down.");
      const root = await realpath(resolve(valid.path));
      if (!(await stat(root)).isDirectory()) throw new Error("Scan path must be a directory.");
      const current = await this.getConfiguration();
      const switched = valid.provider !== undefined && valid.provider !== current.provider;
      const merged = {...current, ...(switched ? {model: defaultModel(valid.provider!), reasoning: undefined} : {}), ...valid};
      const settings = settingsSchema.parse({...merged, reasoning: merged.reasoning ?? undefined});
      const credentials = await readCredentials(this.home);
      if (this.checkProvider) await requireProvider(settings.provider, credentials);
      const client = this.clientFactory(settings, effectiveCredentials(credentials));
      const report = newReport(root, settings.model);
      report.provider = settings.provider;
      initializeExecution(report, settings);
      await new Store(root, this.home).save(report);
      this.launch(report, settings, client);
      return structuredClone(report);
    });
  }
  async resumeScan(id: string, options: {steps?: number} = {}): Promise<Report> {
    const valid = z.object({steps: z.number().int().min(1).max(30).optional()}).strict().parse(options);
    return this.exclusive(async () => {
      if (this.closing) throw new Error("The scanner is shutting down.");
      if (this.active.has(id)) throw new Error("This scan is already running.");
      const report = await this.getScan(id), store = new Store(report.root, this.home);
      if (await store.isRunning(id)) throw new Error("This scan is running in another process.");
      if (report.state === "complete") throw new Error("This scan is already complete. Start a new scan to review changes.");
      if (!report.execution || report.execution.planVersion !== 2) throw new Error("This scan has no resumable checkpoint. Start a new scan.");
      const settings = settingsSchema.parse({...await this.getConfiguration(), ...report.execution, provider: report.provider ?? "openai", model: report.model, reasoning: report.execution.reasoning, steps: valid.steps ?? report.execution.steps});
      const credentials = await readCredentials(this.home);
      if (this.checkProvider) await requireProvider(settings.provider, credentials);
      const client = this.clientFactory(settings, effectiveCredentials(credentials));
      report.execution.steps = settings.steps;
      report.state = "running"; report.phase = "Resuming scan"; report.finishedAt = undefined; report.error = undefined;
      this.launch(report, settings, client);
      return structuredClone(report);
    });
  }
  private launch(report: Report, settings: Settings, client: AgentClient): void {
    const abort = new AbortController(), control = new ScanControl();
    const store = new Store(report.root, this.home);
    const entry: ActiveScan = {report, abort, control, done: Promise.resolve(report)};
    this.active.set(report.id, entry);
    entry.done = scan({report, store, client, signal: abort.signal, control, ...settings, onChange() {}})
      .catch(error => {
        // Lock acquisition can fail before the scanner's own error handler. Never overwrite another owner's report.
        report.state = "failed"; report.phase = "Scan could not start";
        report.error = error instanceof Error ? error.message : "Scan failed.";
        throw error;
      })
      .finally(() => { this.active.delete(report.id); });
    // Background failures are observed even when a dashboard caller does not await the scan.
    void entry.done.catch(() => {});
  }
  async waitForScan(id: string): Promise<Report> {
    const active = this.active.get(id);
    return active ? structuredClone(await active.done) : this.getScan(id);
  }
  async cancelScan(id: string): Promise<Report> {
    const active = this.active.get(id);
    if (!active) {
      const report = await this.getScan(id);
      if (report.state === "running") throw new Error("This scan belongs to another process. Stop it from its original terminal.");
      throw new Error("This scan is not running.");
    }
    active.abort.abort(new Error("Scan cancelled by the user."));
    return structuredClone(await active.done);
  }
  async shutdown(): Promise<void> {
    this.closing = true;
    await this.operations;
    const entries = [...this.active.values()];
    for (const entry of entries) entry.abort.abort(new Error("Scanner shutting down."));
    await Promise.allSettled(entries.map(entry => entry.done));
  }
}
