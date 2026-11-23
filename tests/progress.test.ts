import { afterEach, expect, test } from "bun:test";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const roots: string[] = [];
afterEach(async () => {
  await Promise.all(roots.splice(0).map(root => rm(root, { recursive: true, force: true })));
});

// Exercise the shipped scan command in a real terminal, including async checkpoint cleanup.
const ptyDriver = String.raw`
import errno, json, os, pty, select, signal, sys, time

pid, master = pty.fork()
if pid == 0:
    os.execv(sys.argv[1], [sys.argv[1], sys.argv[2]])

output = bytearray()
sent = False
status = None
deadline = time.monotonic() + 15
try:
    while time.monotonic() < deadline:
        ready, _, _ = select.select([master], [], [], 0.05)
        if ready:
            try:
                chunk = os.read(master, 65536)
            except OSError as error:
                if error.errno != errno.EIO:
                    raise
                chunk = b""
            output.extend(chunk)
            if not sent and b"AGENT_READY" in output:
                os.write(master, b"\x03")
                sent = True
        ended, child_status = os.waitpid(pid, os.WNOHANG)
        if ended:
            status = child_status
            break
    if status is None:
        raise RuntimeError("PTY cancellation did not finish: " + output.decode(errors="replace"))
    print(json.dumps({"sent": sent, "exitCode": os.waitstatus_to_exitcode(status), "output": output.decode(errors="replace")}))
finally:
    if status is None:
        # pty.fork gives the fixture its own session. Kill the whole group on
        # timeout/error so descendants cannot keep descriptors or tests alive.
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
    os.close(master)
`;

test.skipIf(process.platform === "win32")("Ctrl+C in the scan command awaits cancellation and persists its checkpoint before exiting", async () => {
  const python = Bun.which("python3");
  if (!python) throw new Error("The terminal regression test requires Python 3's standard-library pty module.");
  const root = await mkdtemp(join(tmpdir(), "openhack-progress-pty-"));
  roots.push(root);
  const project = join(root, "project");
  const home = join(root, "store");
  const checkpoint = join(root, "observed-checkpoint.json");
  const fixture = join(root, "progress-fixture.ts");
  await mkdir(project);
  await writeFile(join(project, "app.ts"), "export const value = 1;\n");
  await writeFile(fixture, `
import { LocalWorkspace } from ${JSON.stringify(resolve("src/core/workspace.ts"))};
import { runCli } from ${JSON.stringify(resolve("src/cli/run.ts"))};

const home = ${JSON.stringify(home)};
const workspace = new LocalWorkspace({ home, clientFactory: () => ({
  async run(options) {
    options.signal.throwIfAborted();
    console.log("AGENT_READY");
    return new Promise((_resolve, reject) => {
      options.signal.addEventListener("abort", () => {
        console.log("AGENT_ABORTED");
        setTimeout(() => {
          console.log("ASYNC_CANCEL_FINISHED");
          reject(options.signal.reason);
        }, 200);
      }, { once: true });
    });
  }
}) });
await runCli(["scan"], workspace);
const persisted = await new LocalWorkspace({home}).listScans();
await Bun.write(${JSON.stringify(checkpoint)}, JSON.stringify({state: persisted[0]?.state, count: persisted.length, raw: !!process.stdin.isRaw}));
console.log("CHECKPOINT_SAVED");
console.log("COMMAND_RETURNED");
`);
  const child = Bun.spawn([python, "-c", ptyDriver, process.execPath, fixture], {
    // The injected agent never uses a provider. Exclude installed provider CLIs
    // so readiness checks cannot depend on the developer's authenticated tools.
    env: { ...process.env, PATH: root },
    cwd: project,
    stdout: "pipe",
    stderr: "pipe",
  });
  const [stdout, stderr, code] = await Promise.all([
    new Response(child.stdout).text(),
    new Response(child.stderr).text(),
    child.exited,
  ]);
  expect(stderr).toBe("");
  expect(code).toBe(0);
  const result = JSON.parse(stdout) as { sent: boolean; exitCode: number; output: string };
  expect(result.sent).toBe(true);
  expect(result.exitCode).toBe(130);
  expect(result.output).toContain("Provider: OpenAI API");
  expect(result.output).toContain("Model:");
  expect(result.output).toContain("Preparing scan");
  expect(result.output).toContain("saving checkpoint");
  expect(result.output).toContain("AGENT_ABORTED");
  expect(result.output).toContain("ASYNC_CANCEL_FINISHED");
  expect(result.output).toContain("CHECKPOINT_SAVED");
  expect(result.output).toContain("COMMAND_RETURNED");
  expect(result.output.indexOf("CHECKPOINT_SAVED")).toBeGreaterThan(result.output.indexOf("ASYNC_CANCEL_FINISHED"));
  expect(JSON.parse(await readFile(checkpoint, "utf8"))).toEqual({ state: "cancelled", count: 1, raw: false });
}, 20_000);
