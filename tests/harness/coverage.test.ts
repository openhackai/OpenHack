import { reviewed, withReviews } from "./review-fixture";
import { test, expect } from "bun:test";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { inventory } from "../../src/harness/project";
import { planSource, coverageSummary } from "../../src/harness/coverage";
import { newReport, scan } from "../../src/harness/scanner";
import { Store } from "../../src/harness/store";
import { OpenAIClient, type AgentClient, type AgentOptions } from "../../src/harness/openai";
const recon = { overview: "Fixture", stack: [], entryPoints: [], trustBoundaries: [], risks: [] };

test("large repository includes manifests and assigns every source byte, including late files", async () => {
  const root = await mkdtemp(join(tmpdir(), "openhack-coverage-"));
  try {
    const body = "// filler\n".repeat(850);
    await Promise.all(Array.from({length: 1147}, (_, i) => writeFile(join(root, `file-${String(i).padStart(4,"0")}.ts`), body)));
    await writeFile(join(root, "package.json"), '{"name":"large-fixture"}');
    const project = await inventory(root);
    expect(project.bytes).toBeGreaterThan(8_000_000); expect(project.limited).toBe(false);
    expect(project.files.size).toBe(1148); expect(project.files.has("package.json")).toBe(true);
    const plan = planSource(project), segments = plan.flat();
    expect(plan.length).toBeGreaterThan(6);
    for (const [path, source] of project.files) {
      const parts = segments.filter(s => s.path === path).sort((a,b)=>a.from-b.from);
      expect(parts[0]!.from).toBe(0); expect(parts.at(-1)!.to).toBe(source.length);
      for (let i=1;i<parts.length;i++) expect(parts[i]!.from).toBe(parts[i-1]!.to);
    }
    expect(plan[0]![0]!.path).toBe("package.json");
    const report = newReport(root,"test"), supplied = new Set<string>();
    const client: AgentClient = { async run<T>(o: AgentOptions<T>) {
      await o.beforeRequest?.();
      for (const match of o.prompt.matchAll(/^SOURCE (.+) · lines /gm)) supplied.add(match[1]!);
      o.onFinish?.("completed");
      return o.schema.parse(withReviews(o.name === "Recon" ? recon : { findings: [] }, o.prompt));
    } };
    await scan({report,store:new Store(root, join(root, ".openhack")),client,steps:8,workers:4,signal:AbortSignal.timeout(15000),onChange(){}});
    expect(report.state).toBe("complete"); expect(supplied.size).toBe(1148);
    expect(supplied.has("file-1146.ts")).toBe(true); expect(report.readFiles.length).toBe(1148);
    expect(coverageSummary(report).completed).toBe(plan.length);
  } finally { await rm(root,{recursive:true,force:true}); }
}, 20000);

test("empty output at tool limit is partial, and retries preserve completed work", async () => {
  const root = await mkdtemp(join(tmpdir(), "openhack-limit-"));
  try {
    await writeFile(join(root,"app.ts"),"export const safe = true;");
    let limited = true;
    const transport = async (_url: string, init: RequestInit) => {
      const body = JSON.parse(String(init.body));
      const output = limited && !body.input.some((i:any)=>i.type === "function_call_output")
        ? [{type:"function_call",name:"list_files",arguments:'{"contains":"","offset":0}',call_id:"c"}]
        : [{type:"message",content:[{type:"output_text",text:JSON.stringify(withReviews(body.text.format.name === "Recon" ? recon : {findings:[]}, body.input[0].content))}]}];
      return Response.json({status:"completed",output});
    };
    const report=newReport(root,"test"),store=new Store(root, join(root, ".openhack")),client=new OpenAIClient("test","test","http://test.invalid",transport);
    await scan({report,store,client,steps:1,signal:new AbortController().signal,onChange(){}});
    expect(report.state).toBe("partial"); expect(report.findings).toEqual([]);
    expect(coverageSummary(report).limited).toBe(4); expect(report.error).toContain("tool-round limit");
    limited=false;
    await scan({report,store,client,steps:1,signal:new AbortController().signal,onChange(){}});
    expect(report.state).toBe("complete"); expect(coverageSummary(report).limited).toBe(0);
  } finally { await rm(root,{recursive:true,force:true}); }
});

test("literal control characters in UTF-8 source do not hide files; minified bundles are excluded", async () => {
  const root = await mkdtemp(join(tmpdir(), "openhack-source-"));
  try {
    await writeFile(join(root,"escape.ts"),"export const sanitize = (s) => s.replace(/\u0000/g, '');");
    await writeFile(join(root,"vendor.min.mjs"),"x".repeat(300000));
    await writeFile(join(root,"binary.ts"),Buffer.alloc(200,0));
    const p=await inventory(root);
    expect(p.files.has("escape.ts")).toBe(true); expect(p.files.has("vendor.min.mjs")).toBe(false);
    expect(p.omissions).toEqual([{path:"binary.ts",reason:"Binary source"}]);
  } finally { await rm(root,{recursive:true,force:true}); }
});

test("coordinator assigns focused recovery agents and stops when recovery makes no progress", async () => {
  for (const recover of [true,false]) {
    const root=await mkdtemp(join(tmpdir(),"openhack-recovery-"));
    try {
      await writeFile(join(root,"app.ts"),"export const safe = true;");
      const report=newReport(root,"test");
      const client:AgentClient={async run<T>(o:AgentOptions<T>){
        await o.beforeRequest?.(); o.onFinish?.("completed");
        if(o.name==="Recon")return o.schema.parse(recon);
        const reviews=reviewed(o.prompt).map(r=>({...r,status:recover && o.name.startsWith("Recover ")?"reviewed":"unreviewed",reason:"Need to trace a guard"}));
        return o.schema.parse({findings:[],reviews});
      }};
      await scan({report,store:new Store(root, join(root, ".openhack")),client,steps:2,workers:4,signal:AbortSignal.timeout(2000),onChange(){}});
      const e=report.execution!;
      expect(e.tasks.filter(t=>t.recoveryOf).length).toBe(3);
      expect(e.tasks.filter(t=>t.role==="research").length).toBe(6);
      expect(report.state).toBe(recover?"complete":"partial");
      expect(coverageSummary(report).planned).toBe(3);
      expect(coverageSummary(report).completed).toBe(recover ? 3 : 0);
      expect(e.events.some(ev=>ev.actor==="coordinator" && ev.text.includes("recovery agents"))).toBe(true);
      if(recover)expect(e.tasks.filter(t=>t.recovered).length).toBe(3);
    } finally {await rm(root,{recursive:true,force:true});}
  }
});

test("recovery replaces a parent budget warning with the remaining source blocker", async () => {
  const root=await mkdtemp(join(tmpdir(),"openhack-recovery-summary-"));
  try {
    await writeFile(join(root,"app.ts"),"export const safe = true;");
    const report=newReport(root,"test");
    const client:AgentClient={async run<T>(o:AgentOptions<T>){
      await o.beforeRequest?.();
      if(o.name==="Recon"){o.onFinish?.("completed");return o.schema.parse(recon);}
      const recovery=o.name.startsWith("Recover ");
      o.onFinish?.(recovery?"completed":"tool_limit");
      return o.schema.parse({findings:[],reviews:reviewed(o.prompt).map(r=>({...r,status:"unreviewed",reason:"Required authorization module is absent"}))});
    }};
    await scan({report,store:new Store(root, join(root, ".openhack")),client,steps:2,workers:4,signal:AbortSignal.timeout(2000),onChange(){}});
    expect(report.state).toBe("partial");
    expect(coverageSummary(report).limited).toBe(3);
    expect(report.error).toContain("3 agent(s) left source objectives unreviewed");
    expect(report.error).not.toContain("tool-round limit");
  } finally {await rm(root,{recursive:true,force:true});}
});

test("production entry points take priority over test files containing api/lib paths", async () => {
  const { sourcePriority } = await import("../../src/harness/coverage");
  expect(sourcePriority("app/api/route.ts")).toBeLessThan(sourcePriority("__tests__/api/route.test.ts"));
  expect(sourcePriority("lib/auth.ts")).toBeLessThan(sourcePriority("docs/lib/auth.md"));
});

test("resume retains the saved source assignment plan", async () => {
  const root = await mkdtemp(join(tmpdir(),"openhack-fixed-plan-"));
  try {
    for(let i=0;i<8;i++) await writeFile(join(root,`source-${i}.ts`),'// content\n'.repeat(1000));
    const report=newReport(root,"test"), store=new Store(root, join(root, ".openhack")); let fail=true;
    const client:AgentClient={async run<T>(o:AgentOptions<T>){
      if(o.name.includes("2/") && fail)throw Error("retry fixture");
      return o.schema.parse(withReviews(o.name==="Recon"?recon:{findings:[]},o.prompt));
    }};
    const run=()=>scan({report,store,client,steps:2,workers:4,signal:AbortSignal.timeout(2000),onChange(){}});
    await run();expect(report.state).toBe("failed");
    const first=report.execution!.tasks.find(t=>t.role==="research")!;
    first.segments!.reverse();const saved=JSON.stringify(first.segments);
    fail=false;await run();expect(report.state).toBe("complete");expect(JSON.stringify(first.segments)).toBe(saved);
  } finally {await rm(root,{recursive:true,force:true});}
});
