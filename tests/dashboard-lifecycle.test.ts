import { afterEach, expect, test } from "bun:test";
import { link, mkdir, mkdtemp, readFile, realpath, rm, stat, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { LocalWorkspace } from "../src/core/workspace";
import { getDashboardStatus, runDashboard, startDashboardBackground, stopDashboard } from "../src/cli/dashboard";
import { startDashboard } from "../src/server";
import { processAlive } from "../src/core/files";

const roots: string[] = [];
const managed: { workspace: LocalWorkspace; port: number }[] = [];
const servers: Bun.Server<void>[] = [];
afterEach(async () => {
  for (const item of managed.splice(0)) await stopDashboard(item.workspace, item.port).catch(() => {});
  for (const server of servers.splice(0)) server.stop(true);
  await Promise.all(roots.splice(0).map(root => rm(root, { recursive: true, force: true })));
});
async function fixture() {
  const home = await realpath(await mkdtemp(join(tmpdir(), "openhack-dashboard-life-")));
  roots.push(home);
  const probe = Bun.serve({ hostname: "127.0.0.1", port: 0, fetch: () => new Response("probe") });
  const port = probe.port!;
  probe.stop(true);
  const workspace = new LocalWorkspace({ home });
  return { home, workspace, port, url: `http://127.0.0.1:${port}`, record: join(home, `dashboard-${port}.json`) };
}

test("managed foreground status and authenticated shutdown keep the token private", async () => {
  const app = await fixture();
  expect((await getDashboardStatus(app.workspace, app.port)).state).toBe("stopped");
  await runDashboard(app.workspace, app.port, true);
  managed.push(app);
  expect((await getDashboardStatus(app.workspace, app.port)).state).toBe("running");
  const runtime = JSON.parse(await readFile(app.record, "utf8"));
  expect((await stat(app.record)).mode & 0o777).toBe(0o600);
  expect(runtime.token).toHaveLength(64);
  const health = await fetch(`${app.url}/api/health`).then(response => response.json());
  expect(health.instanceId).toBe(runtime.instanceId);
  expect(health.token).toBeUndefined();
  const headers = { "Content-Type": "application/json", "X-OpenHack-Client": "dashboard" };
  expect((await fetch(`${app.url}/api/shutdown`, { method: "POST", headers, body: JSON.stringify({ instanceId: runtime.instanceId }) })).status).toBe(403);
  expect((await fetch(`${app.url}/api/shutdown`, { method: "POST", headers: { ...headers, "X-OpenHack-Token": runtime.token }, body: JSON.stringify({ instanceId: crypto.randomUUID() }) })).status).toBe(403);
  expect((await fetch(`${app.url}/api/shutdown`, { method: "POST", headers: { ...headers, Origin: "https://untrusted.example", "X-OpenHack-Token": runtime.token }, body: JSON.stringify({ instanceId: runtime.instanceId }) })).status).toBe(403);
  expect((await getDashboardStatus(app.workspace, app.port)).state).toBe("running");
  expect((await stopDashboard(app.workspace, app.port)).state).toBe("stopped");
  expect(await stat(app.record).catch(() => null)).toBeNull();
});

test("does not stop unrelated services or older unmanaged dashboards", async () => {
  const app = await fixture();
  const unrelated = Bun.serve({ hostname: "127.0.0.1", port: app.port, fetch: () => Response.json({ app: "another-service" }) });
  expect((await getDashboardStatus(app.workspace, app.port)).state).toBe("occupied");
  await expect(stopDashboard(app.workspace, app.port)).rejects.toThrow("another service");
  unrelated.stop(true);
  const unmanaged = startDashboard({ workspace: app.workspace, port: app.port });
  servers.push(unmanaged);
  expect((await getDashboardStatus(app.workspace, app.port)).state).toBe("unmanaged");
  await expect(stopDashboard(app.workspace, app.port)).rejects.toThrow("original terminal");
  expect((await fetch(`${app.url}/api/health`)).status).toBe(200);
});

test("shutdown cancels owned scans and saves their interrupted state", async () => {
  const app = await fixture();
  const project = join(app.home, "project");
  await mkdir(project);
  await writeFile(join(project, "app.ts"), "export const value = 1;\n");
  let markEntered!: () => void;
  const entered = new Promise<void>(resolveEntered => { markEntered = resolveEntered; });
  const workspace = new LocalWorkspace({ home: app.home, clientFactory: () => ({ async run(options) {
    options.signal.throwIfAborted();
    markEntered();
    await new Promise<never>((_resolve, reject) => options.signal.addEventListener("abort", () => reject(options.signal.reason), { once: true }));
    throw new Error("unreachable");
  } }) });
  await runDashboard(workspace, app.port, true);
  managed.push({ workspace, port: app.port });
  const scan = await workspace.startScan({ path: project });
  await entered;
  expect((await getDashboardStatus(workspace, app.port)).activeScans).toBe(1);
  expect((await stopDashboard(workspace, app.port)).state).toBe("stopped");
  expect((await app.workspace.getScan(scan.id)).state).toBe("cancelled");
});

test("stale ownership records cannot authorize shutdown of a replacement server", async () => {
  const app = await fixture();
  await runDashboard(app.workspace, app.port, true);
  managed.push(app);
  const original = await readFile(app.record, "utf8");
  const wrong = { ...JSON.parse(original), instanceId: crypto.randomUUID() };
  await writeFile(app.record, JSON.stringify(wrong));
  expect((await getDashboardStatus(app.workspace, app.port)).state).toBe("unmanaged");
  await expect(stopDashboard(app.workspace, app.port)).rejects.toThrow("original terminal");
  await writeFile(app.record, original);
  expect((await stopDashboard(app.workspace, app.port)).state).toBe("stopped");
});

test("starts a detached dashboard with private logs and stops its exact instance", async () => {
  const app = await fixture();
  const entrypoint = join(app.home, "runner.ts");
  await writeFile(entrypoint, [
    `import { LocalWorkspace } from ${JSON.stringify(resolve("src/core/workspace.ts"))};`,
    `import { runDashboard } from ${JSON.stringify(resolve("src/cli/dashboard.ts"))};`,
    'const port = Number(process.argv[process.argv.indexOf("--port") + 1]);',
    'const home = process.argv[process.argv.indexOf("--home") + 1];',
    'await runDashboard(new LocalWorkspace({ home }), port, true);',
  ].join("\n"));
  expect((await startDashboardBackground(app.workspace, app.port, { entrypoint, open: false })).state).toBe("running");
  managed.push(app);
  const first = JSON.parse(await readFile(app.record, "utf8"));
  expect(first.pid).not.toBe(process.pid);
  expect((await stat(join(app.home, `dashboard-${app.port}.log`))).mode & 0o777).toBe(0o600);
  expect((await startDashboardBackground(app.workspace, app.port, { entrypoint, open: false })).state).toBe("running");
  expect(JSON.parse(await readFile(app.record, "utf8")).instanceId).toBe(first.instanceId);
  expect((await stopDashboard(app.workspace, app.port)).state).toBe("stopped");
});

test("refuses symlink log targets before spawning a background process", async () => {
  const app = await fixture();
  const protectedPath = join(app.home, "protected.txt");
  await writeFile(protectedPath, "unchanged");
  await symlink(protectedPath, join(app.home, `dashboard-${app.port}.log`));
  await expect(startDashboardBackground(app.workspace, app.port, { entrypoint: "missing.ts", open: false })).rejects.toThrow();
  expect(await readFile(protectedPath, "utf8")).toBe("unchanged");
  expect((await getDashboardStatus(app.workspace, app.port)).state).toBe("stopped");
});

test("a timed-out background launch terminates its own stubborn child", async () => {
  const app = await fixture();
  const pidFile = join(app.home, "spawned.pid"), entrypoint = join(app.home, "stalled.ts");
  await writeFile(entrypoint, `await Bun.write(${JSON.stringify(pidFile)}, String(process.pid));\nprocess.on("SIGTERM", () => {});\nsetInterval(() => {}, 1000);\n`);
  await expect(startDashboardBackground(app.workspace, app.port, { entrypoint, open: false, startupTimeoutMs: 1000 })).rejects.toThrow("deadline");
  const pid = Number(await readFile(pidFile, "utf8"));
  expect(processAlive(pid)).toBe(false);
  expect((await getDashboardStatus(app.workspace, app.port)).state).toBe("stopped");
}, 10_000);

test("background logs cannot overwrite a hard-linked file", async () => {
  const app = await fixture(), protectedPath = join(app.home, "protected.txt");
  await writeFile(protectedPath, "unchanged");
  await link(protectedPath, join(app.home, `dashboard-${app.port}.log`));
  await expect(startDashboardBackground(app.workspace, app.port, { entrypoint: "missing.ts", open: false })).rejects.toThrow("without hard links");
  expect(await readFile(protectedPath, "utf8")).toBe("unchanged");
});
