import { mkdir, mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { version } from "../package.json";

const repo = resolve(import.meta.dir, "..");
const root = await mkdtemp(join(tmpdir(), "openhack-package-"));
const home = join(root, "home");
const archive = join(root, "openhack.tgz");
const executable = join(root, "package/bin/openhack");
let port: number | undefined;
const env = { ...process.env, OPENHACK_HOME: home, OPENAI_API_KEY: "", ANTHROPIC_API_KEY: "", PATH: dirname(process.execPath) };
function assert(value: unknown, message: string): asserts value { if (!value) throw new Error(message); }
async function run(cmd: string[], cwd = root, commandEnv: NodeJS.ProcessEnv = env) {
  const child = Bun.spawn(cmd, { cwd, env: commandEnv, stdout: "pipe", stderr: "pipe" });
  const deadline = setTimeout(() => child.kill(), 30_000);
  try {
    const [out, err, code] = await Promise.all([new Response(child.stdout).text(), new Response(child.stderr).text(), child.exited]);
    assert(code === 0, `${cmd.slice(1, 3).join(" ")} failed (${code}): ${err}`);
    return out;
  } finally { clearTimeout(deadline); }
}
const cli = (...args: string[]) => run([process.execPath, executable, ...args]);
try {
  await mkdir(home);
  // check/build runs first; suppress the lifecycle script to verify that exact build.
  await run([process.execPath, "pm", "pack", "--ignore-scripts", "--filename", archive], repo);
  const tar = Bun.which("tar"); assert(tar, "Package smoke test requires tar.");
  // GNU tar invokes gzip from PATH. Archive tooling uses the build environment;
  // the installed CLI below still runs with only Bun on PATH.
  const archiveEnv = { ...env, PATH: process.env.PATH };
  const entries = (await run([tar, "-tzf", archive], root, archiveEnv)).trim().split("\n");
  assert(entries.every(entry => /^package\/(bin\/|dist\/|README\.md$|LICENSE$|THIRD_PARTY_NOTICES\.md$|package\.json$)/.test(entry)), "Unexpected source or secret in package.");
  assert(entries.includes("package/dist/web/index.html") && entries.includes("package/dist/cli.js"), "Missing build output.");
  await run([tar, "-xzf", archive, "-C", root], root, archiveEnv);
  assert((await cli("version")).trim() === version, "Wrong package version.");
  assert((await cli("help", "scan")).includes("--claude-code"), "Missing command help.");
  assert((await cli("scans", "--json")).trim() === "[]", "Unexpected scan data.");
  const probe = Bun.serve({ hostname: "127.0.0.1", port: 0, fetch: () => new Response() });
  port = probe.port!; await probe.stop(true);
  await cli("--no-open", "--port", String(port));
  const status = JSON.parse(await cli("dashboard", "status", "--port", String(port), "--json"));
  assert(status.state === "running", "Background dashboard did not start.");
  const url = `http://127.0.0.1:${port}`;
  const response = await fetch(url); const html = await response.text();
  assert(response.ok && html.includes("OpenHack"), "Packaged dashboard not served.");
  const assets = [...html.matchAll(/(?:src|href)="(\/assets\/[^\"]+)"/g)].map(match => match[1]!);
  assert(assets.length >= 2, "Missing dashboard JS/CSS.");
  for (const asset of assets) assert((await fetch(url + asset)).ok, `Missing asset ${asset}`);
  const first = await fetch(`${url}/api/health`).then(result => result.json());
  await cli("dashboard", "start", "--no-open", "--port", String(port));
  const second = await fetch(`${url}/api/health`).then(result => result.json());
  assert(first.instanceId === second.instanceId, "Reopening spawned a duplicate server.");
  const stopped = JSON.parse(await cli("dashboard", "stop", "--port", String(port), "--json"));
  assert(stopped.state === "stopped", "Packaged server did not stop.");
  assert(await readFile(join(root, "package/dist/web/fonts/OFL.txt"), "utf8"), "Missing font license.");
  console.log(`Package smoke test passed: ${entries.length} files, standalone CLI, dashboard assets, start/reuse/status/stop.`);
} finally {
  if (port) await cli("dashboard", "stop", "--port", String(port), "--json").catch(() => {});
  await rm(root, { recursive: true, force: true });
}
