import { spawn } from "node:child_process";

export interface CliRequest {
  command: "codex" | "claude";
  args: string[];
  cwd: string;
  input?: string;
  env: NodeJS.ProcessEnv;
  signal: AbortSignal;
  timeoutMs?: number;
  onStdoutLine?: (line: string) => void;
}
export interface CliResult { stdout: string; stderr: string; exitCode: number }
export type CliRunner = (request: CliRequest) => Promise<CliResult>;

/** Pass only OS/runtime locations. API credentials, endpoint overrides, preload hooks, and nesting flags stay out. */
export function subscriptionEnvironment(source: NodeJS.ProcessEnv = process.env): NodeJS.ProcessEnv {
  const keys = ["PATH", "HOME", "USERPROFILE", "USER", "LOGNAME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "LC_CTYPE", "SystemRoot", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR"];
  const env: NodeJS.ProcessEnv = {};
  for (const key of keys) if (source[key] !== undefined) env[key] = source[key];
  return {...env, NO_COLOR: "1", TERM: "dumb", DISABLE_TELEMETRY: "1", CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: "1", CLAUDE_CODE_SAFE_MODE: "1"};
}

/** No shell; bounded output and deadline; cancellation terminates and reaps the entire child group. */
export const runCli: CliRunner = request => new Promise((resolve, reject) => {
  request.signal.throwIfAborted();
  const child = spawn(request.command, request.args, {
    cwd: request.cwd, env: request.env, shell: false, detached: process.platform !== "win32", stdio: ["pipe", "pipe", "pipe"], windowsHide: true,
  });
  let stdout = "", stderr = "", lineBuffer = "", bytes = 0, failure: Error | undefined;
  let escalation: ReturnType<typeof setTimeout> | undefined;
  const killGroup = (signal: NodeJS.Signals) => {
    try { if (process.platform !== "win32" && child.pid) process.kill(-child.pid, signal); else child.kill(signal); } catch { /* Child may already have exited. */ }
  };
  const stop = (error: Error) => {
    if (failure) return;
    failure = error; killGroup("SIGTERM");
    escalation = setTimeout(() => killGroup("SIGKILL"), 1000);
  };
  const aborted = () => stop(new Error("Subscription CLI request cancelled."));
  request.signal.addEventListener("abort", aborted, {once: true});
  if (request.signal.aborted) aborted();
  const deadline = setTimeout(() => stop(new Error("Subscription CLI request timed out.")), request.timeoutMs ?? 300_000);
  const cleanup = () => { clearTimeout(deadline); clearTimeout(escalation); request.signal.removeEventListener("abort", aborted); };
  const receive = (chunk: string, channel: "stdout" | "stderr") => {
    bytes += Buffer.byteLength(chunk);
    if (bytes > 8 * 1024 * 1024) { stop(new Error("Subscription CLI output exceeded its limit.")); return; }
    const text = chunk;
    if (channel === "stderr") { stderr += text; return; }
    stdout += text; lineBuffer += text;
    for (;;) {
      const end = lineBuffer.indexOf("\n"); if (end < 0) break;
      const line = lineBuffer.slice(0, end); lineBuffer = lineBuffer.slice(end + 1);
      try { request.onStdoutLine?.(line); }
      catch (error) { stop(error instanceof Error ? error : new Error("Subscription CLI emitted an invalid event.")); }
    }
  };
  child.stdout.setEncoding("utf8");
  child.stderr.setEncoding("utf8");
  child.stdout.on("data", chunk => receive(chunk, "stdout"));
  child.stderr.on("data", chunk => receive(chunk, "stderr"));
  child.on("error", () => { failure ??= new Error(`${request.command} could not start. Install the official CLI and make it available on PATH.`); });
  child.stdin.on("error", () => { /* EPIPE is reported through the child exit result. */ });
  child.on("close", code => {
    if (lineBuffer.trim()) {
      try { request.onStdoutLine?.(lineBuffer); } catch (error) { failure ??= error instanceof Error ? error : new Error("Invalid CLI output."); }
    }
    // The leader can exit while a detached descendant has closed its inherited pipes.
    // This also covers an intercepted final event without a trailing newline.
    if (failure) killGroup("SIGKILL");
    cleanup();
    if (failure) reject(failure); else resolve({stdout, stderr, exitCode: code ?? 1});
  });
  child.stdin.end(request.input ?? "");
});
