import { afterEach, expect, test } from "bun:test";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { LocalWorkspace } from "../src/core/workspace";
import { stopDashboard } from "../src/cli/dashboard";
const roots: string[] = [];
afterEach(async () => {await Promise.all(roots.splice(0).map(root => rm(root,{recursive:true,force:true})));});
async function run(args: string[], home?: string) {
  const cwd=home ?? await mkdtemp(join(tmpdir(),"openhack-cli-"));
  if (!home) roots.push(cwd);
  const child = Bun.spawn([Bun.which("bun")!,resolve(import.meta.dir,"../src/cli/index.ts"),...args],{cwd,env:{...process.env,PATH:cwd,OPENHACK_HOME:cwd,OPENAI_API_KEY:"",OPENAI_MODEL:"gpt-5-mini",OPENAI_REASONING_EFFORT:"high"},stdout:"pipe",stderr:"pipe"});
  return {code:await child.exited,out:await new Response(child.stdout).text(),err:await new Response(child.stderr).text(),home:cwd};
}
test("CLI works outside its source directory and keeps JSON stdout clean",async()=>{
  const result=await run(["scans","--json"]);
  expect(result.code).toBe(0);expect(JSON.parse(result.out)).toEqual([]);expect(result.err).toBe("");
}, 30_000);
test("default command starts and reuses the dashboard without a terminal menu",async()=>{
  const home=await mkdtemp(join(tmpdir(),"openhack-default-")); roots.push(home);
  const probe=Bun.serve({hostname:"127.0.0.1",port:0,fetch:()=>new Response()});
  const port=probe.port!; await probe.stop(true);
  const workspace=new LocalWorkspace({home});
  try {
    const first=await run(["--no-open","--port",String(port)],home);
    expect(first.code).toBe(0);expect(first.out).toContain(`OpenHack dashboard: http://127.0.0.1:${port}`);
    expect(first.out).not.toContain("What would you like");expect(first.err).toBe("");
    const initial=await fetch(`http://127.0.0.1:${port}/api/health`).then(r=>r.json());
    const second=await run(["--no-open","--port",String(port)],home);
    expect(second.code).toBe(0);expect(second.out).toBe(first.out);
    const reused=await fetch(`http://127.0.0.1:${port}/api/health`).then(r=>r.json());
    expect(reused.instanceId).toBe(initial.instanceId);
  } finally { await stopDashboard(workspace,port); }
}, 30_000);
test("CLI settings persist and explicit default reasoning overrides the environment",async()=>{
  const first=await run(["config","set","workers","3"]);
  expect(first.code).toBe(0);expect(JSON.parse(first.out).workers).toBe(3);
  const next=await run(["config","set","reasoning","default"],first.home);
  expect(next.code).toBe(0);expect(JSON.parse(next.out).reasoning).toBeUndefined();
  const saved=await run(["config"],first.home);expect(JSON.parse(saved.out).workers).toBe(3);expect(JSON.parse(saved.out).reasoning).toBeUndefined();
}, 30_000);
test("CLI missing credentials and invalid flags fail without emitting a false report",async()=>{
  const result=await run(["scan",".","--json"]);
  expect(result.code).toBe(1);expect(result.out).toBe("");expect(result.err).toContain("OPENAI_API_KEY");
  const invalid=await run(["scan",".","--workers","0","--json"]);
  expect(invalid.code).toBe(1);expect(invalid.out).toBe("");
}, 30_000);

test("help topics, aliases and version work without initializing a workspace", async () => {
  for (const args of [["help", "scan"], ["scan", "--help"], ["help", "history"]]) {
    const result = await run(args);
    expect(result.code).toBe(0);
    expect(result.out).toContain("Usage: openhack");
    expect(result.err).toBe("");
  }
  const version = await run(["version"]);
  expect(version.out.trim()).toBe((await import("../package.json")).version);
  expect((await run(["help", "missing"])).code).toBe(1);
}, 30_000);

test("ignored options, conflicting providers and extra arguments fail before scanning", async () => {
  const invalid = [
    ["scan", "--codex", "--claude-code"],
    ["scan", "--codex", "--provider", "codex"],
    ["scan", ".", "extra"],
    ["scan", "--all"],
    ["scans", ".", "--all"],
    ["scans", "--codex"],
    ["resume", "missing", "--codex"],
    ["dashboard", "bad-action"],
    ["dashboard", "stop", "--no-open"],
    ["config", "set", "model", "test", "extra"],
  ];
  for (const args of invalid) {
    const result = await run(args);
    expect(result.code).toBe(1);
    expect(result.out).toBe("");
    expect(result.err).toContain("OpenHack:");
    expect(await new LocalWorkspace({ home: result.home }).listScans()).toEqual([]);
  }
}, 30_000);
