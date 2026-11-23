import { readdir } from "node:fs/promises";
import { createHash } from "node:crypto";
import { join, resolve } from "node:path";
import { z } from "zod";
import { reportSchema, triageSchema, type Report, type TriageMap, type Triage } from "./types";
import { atomicJson, dataHome, isMissing, privateDirectory, processAlive, processLock, readRegular, regularDirectory } from "../core/files";

const idSchema = z.string().regex(/^[a-zA-Z0-9-]+$/, "Invalid scan ID.");
export const projectId = (root: string): string => createHash("sha256").update(resolve(root)).digest("hex").slice(0, 32);
export class Store {
  readonly dir: string;
  readonly home: string;
  readonly root: string;
  constructor(root: string, home?: string) {
    this.root = resolve(root); this.home = dataHome(home);
    this.dir = join(this.home, "projects", projectId(this.root));
  }
  private async ensure() {
    await privateDirectory(this.home);
    await privateDirectory(join(this.home, "projects"));
    await privateDirectory(this.dir);
    await privateDirectory(join(this.dir, "scans"));
  }
  private async checkDirectories() {
    for (const path of [this.home, join(this.home, "projects"), this.dir, join(this.dir, "scans")]) await regularDirectory(path);
  }
  async lock(id?: string) {
    await this.ensure();
    return processLock(id ? join(this.dir, "scans", `${idSchema.parse(id)}.lock`) : join(this.dir, "scan.lock"));
  }
  async isRunning(id: string): Promise<boolean> {
    idSchema.parse(id);
    try { await this.checkDirectories(); return processAlive(Number((await readRegular(join(this.dir, "scans", `${id}.lock`))).split(":")[0])); }
    catch (error) { if (isMissing(error)) return false; throw error; }
  }
  async save(report: Report) {
    await this.ensure(); idSchema.parse(report.id);
    if (resolve(report.root) !== this.root) throw new Error("Report project does not match its store.");
    await atomicJson(join(this.dir, "project.json"), { root: this.root });
    await atomicJson(join(this.dir, "scans", `${report.id}.json`), reportSchema.parse(report), { durable: report.state !== "running" });
  }
  async history(): Promise<Report[]> {
    let names: string[];
    try { await this.checkDirectories(); names = await readdir(join(this.dir, "scans")); }
    catch (error) { if (isMissing(error)) return []; throw error; }
    const reports = await Promise.all(names.filter(n => /^[a-zA-Z0-9-]+\.json$/.test(n)).map(async name => {
      const report = reportSchema.parse(JSON.parse(await readRegular(join(this.dir, "scans", name))));
      if (resolve(report.root) !== this.root) throw new Error("Stored report has an invalid project root.");
      return report;
    }));
    return reports.sort((a, b) => b.startedAt.localeCompare(a.startedAt));
  }
  async latest(id?: string): Promise<Report> {
    const reports = await this.history();
    const report = id ? reports.find(r => r.id === idSchema.parse(id)) : reports[0];
    if (!report) throw new Error(id ? `Scan ${id} not found.` : "No saved scans. Run openhack scan first.");
    return report;
  }
  async triage(): Promise<TriageMap> {
    try { await this.checkDirectories(); return z.record(z.string(), triageSchema).parse(JSON.parse(await readRegular(join(this.dir, "triage.json")))); }
    catch (error) { if (isMissing(error)) return {}; throw error; }
  }
  async update(ids: string[], change: Partial<Pick<Triage, "status" | "note">>): Promise<TriageMap> {
    await this.ensure();
    const release = await processLock(join(this.dir, "triage.lock"));
    try {
      const all = await this.triage();
      for (const id of ids) {
        if (!/^[a-zA-Z0-9-]+$/.test(id)) throw new Error("Invalid finding ID.");
        all[id] = triageSchema.parse({ status: "open", note: "", ...all[id], ...change, updatedAt: new Date().toISOString() });
      }
      await atomicJson(join(this.dir, "triage.json"), all, { durable: true }); return all;
    } finally { await release(); }
  }
}
export async function projectStores(home?: string): Promise<Store[]> {
  const base = dataHome(home), projects = join(base, "projects");
  let names: string[];
  try { await regularDirectory(base); await regularDirectory(projects); names = await readdir(projects); } catch (error) { if (isMissing(error)) return []; throw error; }
  const stores: Store[] = [];
  for (const name of names.filter(n => /^[a-f0-9]{32}$/.test(n))) {
    await regularDirectory(join(projects, name));
    let data: string;
    try { data = await readRegular(join(projects, name, "project.json")); }
    catch (error) { if (isMissing(error)) continue; throw error; }
    const metadata = z.object({root: z.string().min(1)}).parse(JSON.parse(data));
    if (projectId(metadata.root) !== name) throw new Error("Stored project metadata does not match its directory.");
    stores.push(new Store(metadata.root, base));
  }
  return stores;
}
