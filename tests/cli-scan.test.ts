import { afterEach, expect, test } from "bun:test";
import { mkdir, mkdtemp, realpath, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { scanOptions, parseCli } from "../src/cli/args";

const roots: string[] = [];
afterEach(async () => { await Promise.all(roots.splice(0).map(root => rm(root, { recursive: true, force: true }))); });

test("provider shortcuts select exactly one provider and preserve explicit scan settings", () => {
  for (const provider of ["codex", "claude-code", "openai", "anthropic"] as const) {
    expect(scanOptions(parseCli(["scan", `--${provider}`, "--model", "test-model", "--workers", "2"]).values))
      .toEqual({ provider, model: "test-model", workers: 2 });
  }
  expect(scanOptions(parseCli(["scan"]).values)).toEqual({});
  expect(() => scanOptions(parseCli(["scan", "--codex", "--claude-code"]).values)).toThrow("Choose one provider");
});

test("actual scan command uses cwd, persists selected providers, and scopes lists", async () => {
  const root = await realpath(await mkdtemp(join(tmpdir(), "openhack-command-"))); roots.push(root);
  const project = join(root, "project"), other = join(root, "other"), home = join(root, "home");
  await mkdir(project); await mkdir(other);
  for (const dir of [project, other]) await writeFile(join(dir, "app.ts"), "export const answer = 42;\n");
  const fixture = join(root, "run.ts");
  await writeFile(fixture, `
import { LocalWorkspace } from ${JSON.stringify(resolve("src/core/workspace.ts"))};
import { runCli } from ${JSON.stringify(resolve("src/cli/run.ts"))};
import { withReviews } from ${JSON.stringify(resolve("tests/harness/review-fixture.ts"))};
const workspace = new LocalWorkspace({home: ${JSON.stringify(home)}, clientFactory: () => ({
  async run(options) {
    options.signal.throwIfAborted();
    await options.beforeRequest?.();
    options.onTool("read_file", {path:"app.ts", start:1});
    const value = options.name === "Recon"
      ? {overview:"Test", stack:["TypeScript"], entryPoints:["app.ts"], trustBoundaries:[], risks:[]}
      : {findings:[]};
    options.onFinish?.("completed");
    return options.schema.parse(withReviews(value, options.prompt));
  }
})});
await runCli(process.argv.slice(2), workspace);
`);
  const invoke = async (args: string[], cwd = project) => {
    const child = Bun.spawn([process.execPath, fixture, ...args], {
      cwd, env: { ...process.env, PATH: root, OPENAI_MODEL: "gpt-5-mini" }, stdout: "pipe", stderr: "pipe",
    });
    const [out, err, code] = await Promise.all([new Response(child.stdout).text(), new Response(child.stderr).text(), child.exited]);
    expect(err).toBe(""); expect(code).toBe(0);
    return JSON.parse(out);
  };
  const first = await invoke(["scan", "--codex", "--json"]);
  expect(first.root).toBe(project); expect(first.provider).toBe("codex"); expect(first.state).toBe("complete");
  const second = await invoke(["scan", other, "--claude-code", "--json"]);
  expect(second.root).toBe(other); expect(second.provider).toBe("claude-code");
  expect((await invoke(["scans", "--json"])).map((scan: {id: string}) => scan.id)).toEqual([first.id]);
  expect((await invoke(["scans", "--all", "--json"]))).toHaveLength(2);
  expect((await invoke(["history", other, "--json"])).map((scan: {id: string}) => scan.id)).toEqual([second.id]);
  expect(await invoke(["findings", "--json"])).toEqual([]);
  const config = await invoke(["config"]);
  expect(config.provider).toBe("openai");
}, 30_000);
