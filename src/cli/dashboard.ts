import { spawn, type ChildProcess } from "node:child_process";
import { constants } from "node:fs";
import { open, unlink } from "node:fs/promises";
import { createConnection } from "node:net";
import { join, resolve } from "node:path";
import { randomBytes } from "node:crypto";
import { z } from "zod";
import type { LocalWorkspace } from "../core/workspace";
import { atomicJson, isMissing, privateDirectory, processAlive, processLock, readRegular, regularDirectory } from "../core/files";
import { startDashboard } from "../server";
import { openBrowser } from "./browser";
import { safe } from "./display";

const runtimeSchema = z.object({
  version: z.literal(1), home: z.string(), port: z.number().int(), pid: z.number().int().positive(),
  instanceId: z.string().uuid(), token: z.string().regex(/^[a-f0-9]{64}$/),
}).strict();
type Runtime = z.infer<typeof runtimeSchema>;
const healthSchema = z.object({ app: z.literal("openhack"), home: z.string(), instanceId: z.string().optional(), activeScans: z.number().int().nonnegative().optional() });

export interface DashboardStatus {
  state: "stopped" | "running" | "unmanaged" | "occupied";
  url: string;
  activeScans: number;
  detail?: string;
}

function checkedPort(port: number): number { return z.number().int().min(1).max(65535).parse(port); }
function runtimePath(home: string, port: number): string { return join(home, `dashboard-${port}.json`); }
function dashboardUrl(port: number): string { return `http://127.0.0.1:${checkedPort(port)}`; }
async function readRuntime(home: string, port: number): Promise<Runtime | undefined> {
  try {
    await regularDirectory(home);
    const parsed = runtimeSchema.safeParse(JSON.parse(await readRegular(runtimePath(home, port), 4096)));
    return parsed.success && parsed.data.home === home && parsed.data.port === port ? parsed.data : undefined;
  } catch { return undefined; }
}
async function withRuntimeLock<T>(home: string, port: number, action: () => Promise<T>): Promise<T> {
  const deadline = Date.now() + 4000;
  for (;;) {
    let release: () => Promise<void>;
    try { release = await processLock(join(home, `dashboard-${port}.state.lock`)); }
    catch (error) {
      if (!(error instanceof Error) || !/Another process owns|Storage recovery is busy/.test(error.message) || Date.now() >= deadline) throw error;
      await Bun.sleep(25); continue;
    }
    try { return await action(); } finally { await release(); }
  }
}
async function portOpen(port: number): Promise<boolean> {
  return new Promise(resolveResult => {
    const socket = createConnection({ host: "127.0.0.1", port });
    const finish = (value: boolean) => { socket.destroy(); resolveResult(value); };
    socket.setTimeout(500, () => finish(true));
    socket.once("connect", () => finish(true));
    socket.once("error", () => finish(false));
  });
}
async function health(port: number) {
  const response = await fetch(`${dashboardUrl(port)}/api/health`, { signal: AbortSignal.timeout(2000), redirect: "error" });
  if (!response.ok) { await response.body?.cancel(); return undefined; }
  const reader = response.body?.getReader();
  if (!reader) return undefined;
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > 16 * 1024) { await reader.cancel(); return undefined; }
      chunks.push(value);
    }
  } finally { reader.releaseLock(); }
  const parsed = healthSchema.safeParse(JSON.parse(Buffer.concat(chunks, size).toString("utf8")));
  return parsed.success ? parsed.data : undefined;
}

export async function getDashboardStatus(workspace: Pick<LocalWorkspace, "home">, port: number): Promise<DashboardStatus> {
  const url = dashboardUrl(port);
  let current: Awaited<ReturnType<typeof health>>;
  try { current = await health(port); }
  catch {
    return await portOpen(port)
      ? { state: "occupied", url, activeScans: 0, detail: "Another service is using this port or the dashboard is not responding." }
      : { state: "stopped", url, activeScans: 0 };
  }
  if (!current || current.home !== workspace.home) return { state: "occupied", url, activeScans: 0, detail: "This port belongs to another service or another OpenHack data directory." };
  const runtime = await readRuntime(workspace.home, port);
  const activeScans = current.activeScans ?? 0;
  if (runtime && runtime.instanceId === current.instanceId && processAlive(runtime.pid)) return { state: "running", url, activeScans };
  return { state: "unmanaged", url, activeScans, detail: "This dashboard was started without local lifecycle controls. Stop it in its original terminal." };
}

/** Opens only an identified OpenHack dashboard belonging to this data directory. */
export async function openDashboard(workspace: Pick<LocalWorkspace, "home">, port: number): Promise<DashboardStatus> {
  const status = await getDashboardStatus(workspace, port);
  if (status.state !== "running" && status.state !== "unmanaged") throw new Error(status.detail ?? "The dashboard is not running.");
  await openBrowser(status.url);
  return status;
}

export async function stopDashboard(workspace: Pick<LocalWorkspace, "home">, port: number): Promise<DashboardStatus> {
  const status = await getDashboardStatus(workspace, port);
  if (status.state === "stopped") return status;
  if (status.state !== "running") throw new Error(status.detail ?? "This dashboard cannot be stopped from here.");
  const runtime = await readRuntime(workspace.home, port);
  if (!runtime) throw new Error("The dashboard owner record changed. Check its status and retry.");
  const response = await fetch(`${status.url}/api/shutdown`, {
    method: "POST", headers: { "Content-Type": "application/json", "X-OpenHack-Client": "dashboard", "X-OpenHack-Token": runtime.token },
    body: JSON.stringify({ instanceId: runtime.instanceId }), signal: AbortSignal.timeout(5000), redirect: "error",
  });
  if (!response.ok) throw new Error("The dashboard owner changed or shutdown was rejected. Check its status and retry.");
  const deadline = Date.now() + 15_000;
  while (Date.now() < deadline) {
    const current = await health(port).catch(() => undefined);
    if (current && current.instanceId !== runtime.instanceId) return getDashboardStatus(workspace, port);
    if (!current) {
      const status = await getDashboardStatus(workspace, port);
      if (status.state === "stopped") {
        const remaining = await readRuntime(workspace.home, port);
        if (remaining?.instanceId !== runtime.instanceId || !processAlive(remaining.pid)) return status;
      }
    }
    await Bun.sleep(100);
  }
  throw new Error("Shutdown was requested, but the dashboard is still finishing its scans. Check its status shortly.");
}

/** Foreground runner shared by explicit dashboard commands and detached launches. */
export async function runDashboard(workspace: LocalWorkspace, port: number, noOpen = false): Promise<Bun.Server<void> | undefined> {
  const url = dashboardUrl(port);
  const runtime: Runtime = { version: 1, home: workspace.home, port, pid: process.pid, instanceId: crypto.randomUUID(), token: randomBytes(32).toString("hex") };
  let server: Bun.Server<void> | undefined;
  let stopping: Promise<void> | undefined;
  const stop = () => {
    stopping ??= (async () => {
      process.removeListener("SIGINT", onSignal);
      process.removeListener("SIGTERM", onSignal);
      try { await workspace.shutdown(); }
      finally {
        await server?.stop(true);
        await withRuntimeLock(workspace.home, port, async () => {
          const current = await readRuntime(workspace.home, port);
          if (current?.instanceId === runtime.instanceId) await unlink(runtimePath(workspace.home, port)).catch(error => { if (!isMissing(error)) throw error; });
        });
      }
    })();
    return stopping;
  };
  const onSignal = () => { void stop().catch(() => console.error("OpenHack could not finish shutting down cleanly.")); };
  await privateDirectory(workspace.home);
  try {
    await withRuntimeLock(workspace.home, port, async () => {
      server = startDashboard({ workspace, port, lifecycle: { home: workspace.home, instanceId: runtime.instanceId, token: runtime.token, activeScans: () => workspace.activeScanCount, shutdown: stop } });
      try { await atomicJson(runtimePath(workspace.home, port), runtime); }
      catch (error) {
        try { await workspace.shutdown(); } finally { await server.stop(true); }
        throw error;
      }
    });
  }
  catch (error) {
    const existing = await getDashboardStatus(workspace, port);
    if (existing.state !== "running" && existing.state !== "unmanaged") throw error;
    console.log(`OpenHack is already running at ${url}`);
    if (!noOpen) await openBrowser(url).catch(() => console.error(`Open ${url} in your browser.`));
    return undefined;
  }
  process.once("SIGINT", onSignal);
  process.once("SIGTERM", onSignal);
  console.log(`OpenHack dashboard: ${url}\nData: ${safe(workspace.home)}\nPress Ctrl+C to stop.`);
  if (!noOpen) await openBrowser(url).catch(() => console.error(`Open ${url} in your browser.`));
  return server;
}

/** Starts the same runner in an independent process that survives its launcher. */
export async function startDashboardBackground(workspace: Pick<LocalWorkspace, "home">, port: number, options: { open?: boolean; entrypoint?: string; startupTimeoutMs?: number } = {}): Promise<DashboardStatus> {
  const startupTimeoutMs = z.number().int().min(100).max(60_000).parse(options.startupTimeoutMs ?? 15_000);
  const current = await getDashboardStatus(workspace, port);
  if (current.state === "running" || current.state === "unmanaged") {
    if (options.open !== false) await openDashboard(workspace, port);
    return current;
  }
  if (current.state === "occupied") throw new Error(current.detail);
  const entrypoint = options.entrypoint ?? process.argv[1];
  if (!entrypoint) throw new Error("Could not locate the OpenHack executable. Run openhack dashboard from a terminal.");
  await privateDirectory(workspace.home);
  const logPath = join(workspace.home, `dashboard-${port}.log`);
  const log = await open(logPath, constants.O_APPEND | constants.O_CREAT | constants.O_WRONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK, 0o600);
  let exited: number | null | undefined;
  let child: ChildProcess | undefined;
  try {
    const info = await log.stat();
    if (!info.isFile() || info.nlink !== 1) throw new Error("Dashboard logs must be stored in a regular file without hard links.");
    await log.chmod(0o600);
    child = spawn(process.execPath, [resolve(entrypoint), "dashboard", "--port", String(port), "--home", workspace.home, "--no-open"], {
      detached: true, stdio: ["ignore", log.fd, log.fd], env: process.env,
    });
    child.once("exit", code => { exited = code; });
    const spawned = child;
    await new Promise<void>((resolveSpawn, reject) => { spawned.once("spawn", resolveSpawn); spawned.once("error", reject); });
    child.unref();
  } catch (error) { if (child) await terminateLaunch(child); throw error; }
  finally { await log.close(); }
  let started: DashboardStatus;
  try {
    const deadline = Date.now() + startupTimeoutMs;
    for (;;) {
      const status = await getDashboardStatus(workspace, port);
      if (status.state === "running") {
        if ((await readRuntime(workspace.home, port))?.pid !== child.pid) await terminateLaunch(child);
        started = status; break;
      }
      if (exited !== undefined) throw new Error(`The dashboard did not start. Check ${safe(logPath)} for details.`);
      if (Date.now() >= deadline) throw new Error(`The dashboard did not start within its deadline. Check ${safe(logPath)} for details.`);
      await Bun.sleep(100);
    }
  } catch (error) { await terminateLaunch(child); throw error; }
  if (options.open !== false) await openDashboard(workspace, port);
  return started;
}

/** Only terminate the process group created by this failed launch, never a stored PID. */
async function terminateLaunch(child: ChildProcess): Promise<void> {
  if (!child.pid || child.exitCode !== null || child.signalCode !== null) return;
  const stop = (signal: NodeJS.Signals) => {
    try {
      if (process.platform === "win32") child.kill(signal);
      else process.kill(-child.pid!, signal);
    } catch (error) { if (!(error && typeof error === "object" && "code" in error && error.code === "ESRCH")) throw error; }
  };
  const wait = (milliseconds: number) => new Promise<void>(resolveWait => {
    if (child.exitCode !== null || child.signalCode !== null) { resolveWait(); return; }
    const done = () => { clearTimeout(timer); child.removeListener("exit", done); resolveWait(); };
    const timer = setTimeout(done, milliseconds);
    child.once("exit", done);
  });
  stop("SIGTERM");
  await wait(1500);
  if (child.exitCode === null && child.signalCode === null) {
    stop("SIGKILL");
    await wait(1000);
  }
}
