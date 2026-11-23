import { afterEach, describe, expect, test } from "bun:test";
import { mkdtemp, writeFile, readFile, stat, rm, symlink, mkdir, realpath } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { LocalWorkspace } from "../../src/core/workspace";
import { Store } from "../../src/harness/store";
import { newReport, initializeExecution } from "../../src/harness/scanner";
import type { AgentClient } from "../../src/harness/openai";
import { withReviews } from "./review-fixture";

const dirs: string[] = [];
const workspaces: LocalWorkspace[] = [];
afterEach(async () => {
  await Promise.all(workspaces.splice(0).map(workspace => workspace.shutdown()));
  await Promise.all(dirs.splice(0).map(dir => rm(dir, {recursive: true, force: true})));
});
async function fixture() {
  const dir = await realpath(await mkdtemp(join(tmpdir(), "openhack-workspace-"))); dirs.push(dir);
  const home = join(dir, "home"), project = join(dir, "project"); await mkdir(project);
  await writeFile(join(project, "app.ts"), "export function query(id) {\n  return db.find(id);\n}\n");
  return {dir, home, project};
}
const candidate = {title: "Unscoped lookup", severity: "high", cwe: "CWE-862", file: "app.ts", line: 2, endLine: 2, evidence: "return db.find(id);", summary: "Missing ownership scope", dataFlow: "id to database", impact: "Cross-tenant access", remediation: "Scope to tenant"};
const client: AgentClient = {async run(options) {
  await options.beforeRequest?.(); options.signal.throwIfAborted();
  options.onTool("read_file", {path: "app.ts", start: 1}); options.onUsage(10, 5); options.onFinish?.("completed");
  const value = options.name === "Recon" ? {overview: "Fixture", stack: ["TypeScript"], entryPoints: ["app.ts"], trustBoundaries: ["Tenant"], risks: []}
    : ["Validation", "Advocacy", "Judgment"].includes(options.name) ? {verdict: "validated", severity: "high", reason: "Read fixture source"}
    : {findings: [candidate]};
  return options.schema.parse(withReviews(value, options.prompt));
}};
function workspace(home: string, override: AgentClient = client) {
  const result = new LocalWorkspace({home, clientFactory: () => override}); workspaces.push(result); return result;
}

describe("local workspace", () => {
  test("real harness persists central reports and triage across scans and independent workspaces", async () => {
    const {home, project} = await fixture(), first = workspace(home);
    const started = await first.startScan({path: project, workers: 2});
    const report = await first.waitForScan(started.id);
    expect(report.state).toBe("complete"); expect(report.findings).toHaveLength(1);
    expect(report.findings[0]!.validation).toBe("validated");
    expect(await stat(join(project, ".openhack")).catch(() => null)).toBeNull();
    const row = (await first.listFindings())[0]!;
    await first.updateFinding(row.scanId, row.id, {status: "resolved", note: "Patched locally"});
    const second = workspace(home);
    expect((await second.getFinding(row.scanId, row.id)).note).toBe("Patched locally");
    const next = await second.startScan({path: project}); await second.waitForScan(next.id);
    const findings = await second.listFindings();
    expect(findings).toHaveLength(1); expect(findings[0]!.scanId).toBe(next.id); expect(findings[0]!.status).toBe("resolved");
    expect(await second.listScans()).toHaveLength(2);
    const storage = new Store(project, home);
    expect((await stat(join(storage.dir, "scans", `${next.id}.json`))).mode & 0o777).toBe(0o600);
    expect((await stat(home)).mode & 0o777).toBe(0o700);
  });
  test("only source-review workers receive review-entry output instructions", async () => {
    const {home, project} = await fixture();
    const prompts: {name: string; prompt: string}[] = [];
    const app = workspace(home, {async run(options) {
      prompts.push({name: options.name, prompt: options.prompt});
      return client.run(options);
    }});
    const scan = await app.startScan({path: project});
    expect((await app.waitForScan(scan.id)).state).toBe("complete");
    const recon = prompts.find(item => item.name === "Recon")!;
    expect(recon.prompt).not.toContain("Return exactly one reviews entry");
    expect(prompts.some(item => item.name !== "Recon" && item.prompt.includes("Return exactly one reviews entry"))).toBe(true);
    for (const item of prompts.filter(item => ["Validation", "Advocacy", "Judgment"].includes(item.name))) {
      expect(item.prompt).not.toContain("Return exactly one reviews entry");
    }
  });
  test("settings persist strict typed fields and never write API credentials", async () => {
    const {home} = await fixture(), app = workspace(home);
    const updated = await app.updateSettings({model: "test-model", port: 490, workers: 6, context: "Review auth"});
    expect(updated.port).toBe(490); expect((await workspace(home).getSettings()).workers).toBe(6);
    const stored = JSON.parse(await readFile(join(home, "settings.json"), "utf8"));
    expect(stored.apiKeyConfigured).toBeUndefined(); expect(stored.home).toBeUndefined();
    await expect(app.updateSettings({workers: 0})).rejects.toThrow();
    await expect(app.updateSettings({apiKey: "forbidden"} as never)).rejects.toThrow();
  });
  test("failed or interrupted runs can resume while live external owners cannot be claimed", async () => {
    const {home, project} = await fixture(), app = workspace(home);
    const stored = new Store(project, home), report = newReport(project, "test");
    initializeExecution(report, {steps: 2}); await stored.save(report);
    const release = await stored.lock(report.id);
    expect((await app.getScan(report.id)).state).toBe("running");
    await expect(app.resumeScan(report.id)).rejects.toThrow("another process");
    await expect(app.cancelScan(report.id)).rejects.toThrow("another process");
    await release();
    expect((await app.getScan(report.id)).state).toBe("cancelled");
    await app.resumeScan(report.id); expect((await app.waitForScan(report.id)).state).toBe("complete");
  });
  test("cancellation aborts requests, saves a checkpoint, and resumes through the same harness", async () => {
    const {home, project} = await fixture(); let hold = true;
    const blocking: AgentClient = {async run(options) {
      if (!hold) return client.run(options);
      options.signal.throwIfAborted();
      await new Promise<void>((_resolve, reject) => options.signal.addEventListener("abort", () => reject(options.signal.reason), {once: true}));
      throw new Error("unreachable");
    }};
    const app = workspace(home, blocking), started = await app.startScan({path: project});
    const cancelled = await app.cancelScan(started.id); expect(cancelled.state).toBe("cancelled");
    hold = false; await app.resumeScan(started.id); expect((await app.waitForScan(started.id)).state).toBe("complete");
  });
  test("separate project roots keep equal finding fingerprints independently triaged", async () => {
    const {home, project, dir} = await fixture(), other = join(dir, "other"); await mkdir(other);
    await writeFile(join(other, "app.ts"), await readFile(join(project, "app.ts")));
    const app = workspace(home);
    const first = await app.startScan({path: project}); await app.waitForScan(first.id);
    const row = (await app.listFindings())[0]!; await app.updateFinding(row.scanId, row.id, {status: "dismissed"});
    const second = await app.startScan({path: other}); await app.waitForScan(second.id);
    const findings = await app.listFindings(); expect(findings).toHaveLength(2);
    expect(findings.find(item => item.root === other)!.status).toBe("open");
    expect((await app.listScans(project)).map(scan => scan.id)).toEqual([first.id]);
    expect((await app.listFindings(project)).map(finding => finding.root)).toEqual([project]);
    expect((await app.listFindings(other)).map(finding => finding.root)).toEqual([other]);
    const alias = join(dir, "project-alias"); await symlink(project, alias);
    expect((await app.listScans(alias)).map(scan => scan.id)).toEqual([first.id]);
    expect((await app.listFindings(alias)).map(finding => finding.root)).toEqual([project]);
  });
  test("explicit provider-default reasoning survives a configured environment default", async () => {
    const {home} = await fixture(), app = workspace(home);
    const previous = process.env.OPENAI_REASONING_EFFORT;
    try {
      process.env.OPENAI_REASONING_EFFORT = "high";
      expect((await app.getSettings()).reasoning).toBe("high");
      await app.updateSettings({reasoning: null});
      expect((await workspace(home).getSettings()).reasoning).toBeUndefined();
    } finally {
      if (previous === undefined) delete process.env.OPENAI_REASONING_EFFORT;
      else process.env.OPENAI_REASONING_EFFORT = previous;
    }
  });
  test("starting an empty new scan keeps the previous findings visible", async () => {
    const {home, project} = await fixture(), app = workspace(home);
    const first = await app.startScan({path: project}); await app.waitForScan(first.id);
    const old = (await app.listFindings())[0]!;
    const report = newReport(project, "test"); initializeExecution(report, {steps: 2});
    await new Store(project, home).save(report);
    expect((await app.listFindings())[0]!.scanId).toBe(old.scanId);
  });
  test("dead process locks are recoverable and live locks reject a second owner", async () => {
    const {home, project} = await fixture(), store = new Store(project, home), report = newReport(project, "test");
    await store.save(report);
    const path = join(store.dir, "scans", `${report.id}.lock`);
    await writeFile(path, "99999999:dead", {mode: 0o600});
    const release = await store.lock(report.id);
    expect(await store.isRunning(report.id)).toBe(true);
    await expect(store.lock(report.id)).rejects.toThrow("Another process");
    await release(); expect(await store.isRunning(report.id)).toBe(false);
  });
  test("storage refuses a symlink home", async () => {
    const {home, dir} = await fixture(), actual = join(dir, "elsewhere"); await mkdir(actual); await symlink(actual, home);
    await expect(workspace(home).updateSettings({workers: 3})).rejects.toThrow("real directory");
  });
});
