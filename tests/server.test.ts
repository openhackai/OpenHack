import { afterEach, describe, expect, test } from "bun:test";
import { mkdtemp, mkdir, rm, symlink, writeFile, stat } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { startDashboard, type DashboardWorkspace } from "../src/server";
import { dashboardFile } from "../src/server/static";
import type { FindingRecord, Report, SettingsView } from "../src/shared/contracts";
import { LocalWorkspace } from "../src/core/workspace";
import type { AgentClient } from "../src/harness/openai";
import { withReviews } from "./harness/review-fixture";
import { version } from "../package.json";

const servers: Bun.Server<void>[] = [];
const directories: string[] = [];
const workspaces: LocalWorkspace[] = [];
afterEach(async () => {
  for (const server of servers.splice(0)) server.stop(true);
  await Promise.all(workspaces.splice(0).map(workspace => workspace.shutdown()));
  await Promise.all(directories.splice(0).map(path => rm(path, { recursive: true, force: true })));
});

const report: Report = {
  version: 1, id: "scan-1", project: "fixture", root: "/fixture", model: "fixture-model",
  startedAt: "2026-01-01T00:00:00.000Z", state: "complete", phase: "Complete",
  inventory: { files: 1, bytes: 12, skipped: 0, limited: false }, readFiles: [], findings: [],
  usage: { input: 0, output: 0, requests: 0 }, events: [],
};
const finding: FindingRecord = {
  key: "scan-1:finding-1", id: "finding-1", scanId: "scan-1", project: "fixture", root: "/fixture",
  title: "Fixture finding", severity: "high", cwe: "CWE-79", file: "app.ts", line: 1, endLine: 1,
  summary: "Fixture", evidence: "Fixture", dataFlow: "Fixture", impact: "Fixture", remediation: "Fixture",
  validation: "validated", validationReason: "Fixture", status: "open", note: "", updatedAt: "",
};
const settings: SettingsView = {
  provider: "openai", model: "fixture-model", workers: 4, steps: 20, context: "", port: 4900,
  home: "/fixture/.openhack", apiKeyConfigured: false,
  providers: [
    { id: "openai", ready: false, detail: "Set OPENAI_API_KEY." },
    { id: "anthropic", ready: false, detail: "Set ANTHROPIC_API_KEY." },
    { id: "codex", ready: false, detail: "Install and log in to Codex." },
    { id: "claude-code", ready: false, detail: "Install and log in to Claude Code." },
  ],
};

function fixtureWorkspace() {
  let changes = 0;
  const workspace: DashboardWorkspace = {
    getSettings: async () => settings,
    updateSettings: async patch => { changes++; return { ...settings, ...patch, reasoning: patch.reasoning ?? undefined }; },
    configureCredential: async () => { changes++; return settings; },
    removeCredential: async () => { changes++; return settings; },
    refreshProviders: async () => settings.providers,
    listScans: async () => [report],
    getScan: async id => { if (id !== report.id) throw new Error(`Scan ${id} not found.`); return report; },
    listFindings: async () => [finding],
    listScanFindings: async () => [finding],
    getFinding: async () => finding,
    updateFinding: async (scanId, findingId, patch) => { changes++; return { ...finding, scanId, id: findingId, ...patch }; },
    startScan: async input => { changes++; return { ...report, root: input.path, state: "running" }; },
    resumeScan: async () => { changes++; return { ...report, state: "running" }; },
    cancelScan: async () => { changes++; return { ...report, state: "cancelled" }; },
  };
  return { workspace, changes: () => changes };
}

async function setup(options: { devOrigins?: string[] } = {}) {
  const root = await mkdtemp(join(tmpdir(), "openhack-server-test-"));
  directories.push(root);
  const webDir = join(root, "web");
  await mkdir(join(webDir, "assets"), { recursive: true });
  await writeFile(join(webDir, "index.html"), "<!doctype html><title>OpenHack fixture</title>");
  await writeFile(join(webDir, "assets/app-test.js"), "console.log('fixture');");
  const fixture = fixtureWorkspace();
  const server = startDashboard({ workspace: fixture.workspace, webDir, port: 0, ...options });
  servers.push(server);
  return { ...fixture, root, webDir, server, url: `http://127.0.0.1:${server.port}` };
}

const mutationHeaders = { "Content-Type": "application/json", "X-OpenHack-Client": "dashboard" };

describe("local dashboard API", () => {
  test("unexpected errors and invalid field names cannot reflect private values", async () => {
    const app = await setup();
    const marker = "PRIVATE_VALUE_MUST_NOT_BE_RETURNED";
    app.workspace.listScans = async () => { throw new Error(marker); };
    const failed = await fetch(`${app.url}/api/scans`);
    expect(failed.status).toBe(500);
    expect(await failed.text()).not.toContain(marker);
    const invalid = await fetch(`${app.url}/api/settings`, { method: "PATCH", headers: mutationHeaders, body: JSON.stringify({ [marker]: true }) });
    expect(invalid.status).toBe(400);
    expect(await invalid.text()).not.toContain(marker);
  });

  test("managed health does not depend on provider probes or persisted scan validity", async () => {
    const fixture = fixtureWorkspace();
    const server = startDashboard({
      workspace: {
        ...fixture.workspace,
        getSettings: async () => { throw new Error("Provider probe unavailable"); },
        listScans: async () => { throw new Error("Invalid saved report"); },
      },
      port: 0,
      lifecycle: { home: settings.home, instanceId: "test-instance", token: "test-token", activeScans: () => 2, shutdown: async () => {} },
    });
    servers.push(server);
    const response = await fetch(`http://127.0.0.1:${server.port}/api/health`);
    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({ home: settings.home, instanceId: "test-instance", activeScans: 2 });
  });

  test("serves persisted state and validates changes before invoking the workspace", async () => {
    const app = await setup();
    const health = await fetch(`${app.url}/api/health`).then(response => response.json());
    expect(health).toEqual({ app: "openhack", version, home: settings.home });
    expect(await fetch(`${app.url}/api/scans`).then(response => response.json())).toEqual({ scans: [report] });
    expect(await fetch(`${app.url}/api/findings/scan-1%3Afinding-1`).then(response => response.json())).toEqual({ finding });
    const invalid = await fetch(`${app.url}/api/settings`, { method: "PATCH", headers: mutationHeaders, body: JSON.stringify({ workers: 33, apiKey: "should-not-be-accepted" }) });
    expect(invalid.status).toBe(400);
    expect(app.changes()).toBe(0);
    const updated = await fetch(`${app.url}/api/findings/scan-1%3Afinding-1`, { method: "PATCH", headers: mutationHeaders, body: JSON.stringify({ status: "resolved", note: "Reviewed" }) });
    expect(updated.status).toBe(200);
    expect((await updated.json()).finding).toMatchObject({ status: "resolved", note: "Reviewed" });
    expect(app.changes()).toBe(1);
    const started = await fetch(`${app.url}/api/scans`, { method: "POST", headers: mutationHeaders, body: JSON.stringify({ path: "/another-fixture" }) });
    expect(started.status).toBe(202);
    expect((await started.json()).scan.root).toBe("/another-fixture");
  });

  test("rejects cross-site access, DNS rebinding hosts, and simple form requests", async () => {
    const app = await setup();
    const requests: Record<string, string>[] = [
      { ...mutationHeaders, Origin: "https://untrusted.example" },
      { ...mutationHeaders, Host: `attacker.example:${app.server.port}` },
      { ...mutationHeaders, "Sec-Fetch-Site": "cross-site" },
      { "Content-Type": "application/json" },
      { "Content-Type": "text/plain", "X-OpenHack-Client": "dashboard" },
    ];
    for (const headers of requests) {
      const response = await fetch(`${app.url}/api/scans`, { method: "POST", headers, body: JSON.stringify({ path: "/fixture" }) });
      expect(response.status).toBe(403);
      expect(response.headers.has("Access-Control-Allow-Origin")).toBe(false);
    }
    expect(app.changes()).toBe(0);
    expect((await fetch(`${app.url}/api/scans`, { headers: { Origin: "null" } })).status).toBe(403);
    expect((await fetch(`${app.url}/api/scans`, { headers: { Origin: app.url } })).status).toBe(200);
  });

  test("allows only explicitly enabled development origins", async () => {
    const app = await setup({ devOrigins: ["http://127.0.0.1:5173"] });
    const response = await fetch(`${app.url}/api/settings`, { method: "OPTIONS", headers: { Origin: "http://127.0.0.1:5173", "Access-Control-Request-Method": "PATCH" } });
    expect(response.status).toBe(204);
    expect(response.headers.get("Access-Control-Allow-Origin")).toBe("http://127.0.0.1:5173");
    expect((await fetch(`${app.url}/api/scans`, { headers: { Origin: "http://localhost:5173" } })).status).toBe(403);
  });

  test("returns useful errors for missing scans, malformed JSON, and wrong methods", async () => {
    const app = await setup();
    expect((await fetch(`${app.url}/api/scans/missing`)).status).toBe(404);
    expect((await fetch(`${app.url}/api/findings`, { method: "DELETE" })).status).toBe(405);
    expect((await fetch(`${app.url}/api/scans`, { method: "POST", headers: mutationHeaders, body: "{" })).status).toBe(400);
    expect((await fetch(`${app.url}/api/findings/bad-key`, { method: "PATCH", headers: mutationHeaders, body: "{}" })).status).toBe(400);
    expect((await fetch(`${app.url}/api/unknown`)).status).toBe(404);
    expect(app.changes()).toBe(0);
  });
});

describe("dashboard assets", () => {
  test("serves the SPA with security headers and keeps missing assets as 404", async () => {
    const app = await setup();
    const page = await fetch(`${app.url}/scans/scan-1`);
    expect(page.status).toBe(200);
    expect(await page.text()).toContain("OpenHack fixture");
    expect(page.headers.get("Content-Security-Policy")).toContain("frame-ancestors 'none'");
    expect(page.headers.get("Cache-Control")).toBe("no-store");
    const asset = await fetch(`${app.url}/assets/app-test.js`);
    expect(asset.status).toBe(200);
    expect(asset.headers.get("Cache-Control")).toContain("immutable");
    expect((await fetch(`${app.url}/assets/missing.js`)).status).toBe(404);
  });

  test("never serves dotfiles, traversal paths, or symlinks outside the web root", async () => {
    const app = await setup();
    await writeFile(join(app.root, "private.txt"), "PRIVATE");
    await writeFile(join(app.webDir, ".env"), "PRIVATE");
    await symlink(join(app.root, "private.txt"), join(app.webDir, "assets/leak.txt"));
    for (const path of ["/.env", "/../private.txt", "/%2e%2e/private.txt", "/assets/leak.txt", "/%00", "/assets\\private.txt"]) {
      expect(await dashboardFile(app.webDir, path)).toBeNull();
    }
    expect((await fetch(`${app.url}/assets/leak.txt`)).status).toBe(404);
  });
});

test("HTTP scan and triage survive a fresh workspace using the real harness and central storage", async () => {
  const root = await mkdtemp(join(tmpdir(), "openhack-http-workspace-"));
  directories.push(root);
  const home = join(root, "home"), project = join(root, "project");
  await mkdir(project);
  await writeFile(join(project, "app.ts"), "export function query(id) {\n  return db.find(id);\n}\n");
  const candidate = {
    title: "Unscoped lookup", severity: "high", cwe: "CWE-862", file: "app.ts", line: 2, endLine: 2,
    evidence: "return db.find(id);", summary: "Missing ownership scope", dataFlow: "id to database",
    impact: "Cross-tenant access", remediation: "Scope to tenant",
  };
  const client: AgentClient = { async run(options) {
    await options.beforeRequest?.();
    options.signal.throwIfAborted();
    options.onTool("read_file", { path: "app.ts", start: 1 });
    options.onUsage(10, 5);
    options.onFinish?.("completed");
    const value = options.name === "Recon"
      ? { overview: "Fixture", stack: ["TypeScript"], entryPoints: ["app.ts"], trustBoundaries: ["Tenant"], risks: [] }
      : ["Validation", "Advocacy", "Judgment"].includes(options.name)
        ? { verdict: "validated", severity: "high", reason: "Read fixture source" }
        : { findings: [candidate] };
    return options.schema.parse(withReviews(value, options.prompt));
  } };
  const workspace = new LocalWorkspace({ home, clientFactory: () => client });
  workspaces.push(workspace);
  const server = startDashboard({ workspace, port: 0 });
  servers.push(server);
  const url = `http://127.0.0.1:${server.port}`;
  const started = await fetch(`${url}/api/scans`, { method: "POST", headers: mutationHeaders, body: JSON.stringify({ path: project, workers: 2 }) });
  expect(started.status).toBe(202);
  const { scan: initial } = await started.json() as { scan: Report };
  await workspace.waitForScan(initial.id);
  const { scan } = await fetch(`${url}/api/scans/${initial.id}`).then(response => response.json()) as { scan: Report };
  expect(scan.state).toBe("complete");
  expect(scan.findings).toHaveLength(1);
  expect(scan.findings[0]!.validation).toBe("validated");
  expect(await stat(join(project, ".openhack")).catch(() => null)).toBeNull();

  const key = `${scan.id}:${scan.findings[0]!.id}`;
  const patched = await fetch(`${url}/api/findings/${encodeURIComponent(key)}`, { method: "PATCH", headers: mutationHeaders, body: JSON.stringify({ status: "resolved", note: "Reviewed through HTTP" }) });
  expect(patched.status).toBe(200);
  const cannotResume = await fetch(`${url}/api/scans/${scan.id}/resume`, { method: "POST", headers: mutationHeaders, body: "{}" });
  expect(cannotResume.status).toBe(409);
  const cannotCancel = await fetch(`${url}/api/scans/${scan.id}/cancel`, { method: "POST", headers: mutationHeaders, body: "{}" });
  expect(cannotCancel.status).toBe(409);

  await fetch(`${url}/api/settings`, { method: "PATCH", headers: mutationHeaders, body: JSON.stringify({ reasoning: "high" }) });
  const cleared = await fetch(`${url}/api/settings`, { method: "PATCH", headers: mutationHeaders, body: JSON.stringify({ reasoning: null }) });
  expect(cleared.status).toBe(200);
  expect((await cleared.json()).settings.reasoning).toBeUndefined();

  await workspace.shutdown();
  server.stop(true);
  const fresh = new LocalWorkspace({ home, clientFactory: () => client });
  workspaces.push(fresh);
  const restarted = startDashboard({ workspace: fresh, port: 0 });
  servers.push(restarted);
  const restartedUrl = `http://127.0.0.1:${restarted.port}`;
  const stored = await fetch(`${restartedUrl}/api/findings/${encodeURIComponent(key)}`).then(response => response.json());
  expect(stored.finding).toMatchObject({ status: "resolved", note: "Reviewed through HTTP" });
  expect((await fetch(`${restartedUrl}/api/scans`).then(response => response.json())).scans).toHaveLength(1);
  expect((await fresh.getSettings()).reasoning).toBeUndefined();
});
