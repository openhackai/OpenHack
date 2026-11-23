import { copyFile, mkdir, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { version } from "../package.json";

const repo = resolve(import.meta.dir, "..");
const root = await mkdtemp(join(tmpdir(), "openhack-standalone-"));
const name = process.platform === "win32" ? "openhack.exe" : "openhack";
const executable = join(root, name);
const binaryArg = process.argv.find(arg => arg.startsWith("--binary="));
const sourceBinary = binaryArg ? resolve(binaryArg.slice("--binary=".length)) : join(repo, "dist", name);
// Windows treats environment variable names case-insensitively. Drop every PATH spelling.
const inheritedEnv = Object.fromEntries(Object.entries(process.env).filter(([key]) => key.toUpperCase() !== "PATH"));
const env = {
  ...inheritedEnv, PATH: join(root, "empty-path"), OPENHACK_HOME: join(root, "home"),
  OPENAI_API_KEY: "", ANTHROPIC_API_KEY: "",
};
let port: number | undefined;
function assert(value: unknown, message: string): asserts value { if (!value) throw new Error(message); }
async function cli(...args: string[]) {
  const child = Bun.spawn([executable, ...args], { cwd: root, env, stdout: "pipe", stderr: "pipe" });
  const deadline = setTimeout(() => child.kill(), 30_000);
  try {
    const [out, err, code] = await Promise.all([
      new Response(child.stdout).text(), new Response(child.stderr).text(), child.exited,
    ]);
    assert(code === 0, `${args.join(" ")} failed (${code}): ${err}`);
    return out;
  } finally { clearTimeout(deadline); }
}

try {
  await mkdir(env.PATH);
  // Only the executable travels: no Bun, source, node_modules or adjacent assets.
  await copyFile(sourceBinary, executable);
  assert((await cli("version")).trim() === version, "Wrong standalone version.");
  assert((await cli("help", "scan")).includes("--claude-code"), "Missing command help.");
  assert((await cli("scans", "--json")).trim() === "[]", "Unexpected scan data.");
  const probe = Bun.serve({ hostname: "127.0.0.1", port: 0, fetch: () => new Response() });
  port = probe.port!; await probe.stop(true);
  await cli("--no-open", "--port", String(port));
  const status = JSON.parse(await cli("dashboard", "status", "--port", String(port), "--json"));
  assert(status.state === "running", "Standalone dashboard did not start.");
  const url = `http://127.0.0.1:${port}`;
  const webDir = join(repo, "dist/web");
  for (const file of new Bun.Glob("**/*").scanSync({ cwd: webDir, onlyFiles: true })) {
    const response = await fetch(`${url}/${file.replaceAll("\\", "/")}`);
    assert(response.ok, `Missing embedded asset: ${file}`);
    const actual = Buffer.from(await response.arrayBuffer());
    const expected = Buffer.from(await Bun.file(join(webDir, file)).arrayBuffer());
    assert(actual.equals(expected), `Incorrect embedded asset: ${file}`);
  }
  const html = await Bun.file(join(webDir, "index.html")).text();
  assert(await fetch(url).then(response => response.text()) === html, "Missing dashboard index.");
  assert(await fetch(`${url}/scans/example`).then(response => response.text()) === html, "Missing SPA fallback.");
  assert((await fetch(`${url}/assets/missing.js`)).status === 404, "Missing asset should return 404.");
  assert((await fetch(`${url}/%2ehidden`)).status === 404, "Hidden paths should return 404.");
  const first = await fetch(`${url}/api/health`).then(response => response.json());
  await cli("dashboard", "start", "--no-open", "--port", String(port));
  const second = await fetch(`${url}/api/health`).then(response => response.json());
  assert(first.instanceId === second.instanceId, "Reopening spawned a duplicate server.");
  const stopped = JSON.parse(await cli("dashboard", "stop", "--port", String(port), "--json"));
  assert(stopped.state === "stopped", "Standalone dashboard did not stop.");
  console.log("Standalone smoke test passed: no Bun on PATH, embedded assets, start/reuse/status/stop.");
} finally {
  if (port) await cli("dashboard", "stop", "--port", String(port), "--json").catch(() => {});
  await rm(root, { recursive: true, force: true });
}
