import { describe, expect, test } from "bun:test";
import { stat, mkdtemp, writeFile, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { z } from "zod";
import { CodexClient, ClaudeCodeClient, probeSubscriptionLogin } from "../../src/providers/subscription";
import { subscriptionEnvironment, runCli, type CliRequest, type CliRunner } from "../../src/providers/cli-process";
import { parseAnswer, inspectEvent, createClaudeProtocolMonitor, ClaudeSourceRequest, ClaudeSourceCorrection } from "../../src/providers/cli-protocol";
import { scanTools, parseScanToolArguments, type ScanToolName, type ScanToolCall } from "../../src/harness/tools";
import { reconSchema } from "../../src/harness/types";
import type { AgentOptions } from "../../src/harness/openai";

const schema = z.object({summary: z.string()});
function options(overrides: Partial<AgentOptions<z.infer<typeof schema>>> = {}): AgentOptions<z.infer<typeof schema>> {
  return {name: "Review", prompt: "Review the supplied source.", schema, signal: new AbortController().signal, steps: 2, onUsage() {}, onEvent() {}, onTool() { return {source: "export const value = 1;"}; }, ...overrides};
}
const envelope = (value: unknown) => ({response: {kind: "result", result: value}});
const sourceEnvelope = (toolCalls: unknown[]) => ({response: {kind: "tools", toolCalls}});
function codexOutput(value: unknown) {
  return [JSON.stringify({type: "thread.started", thread_id: "test"}), JSON.stringify({type: "item.completed", item: {type: "agent_message", text: JSON.stringify(value)}}), JSON.stringify({type: "turn.completed", usage: {input_tokens: 13, output_tokens: 7, cached_input_tokens: 2}})].join("\n");
}
function claudeOutput(value: unknown) {
  return [JSON.stringify({type: "system", subtype: "init", tools: ["StructuredOutput"], mcp_servers: []}), JSON.stringify({type: "result", subtype: "success", is_error: false, structured_output: value, usage: {input_tokens: 9, output_tokens: 6, cache_creation_input_tokens: 3, cache_read_input_tokens: 2}, permission_denials: []})].join("\n");
}
function runner(outputs: string[], provider: "codex" | "claude", requests: CliRequest[] = []): CliRunner {
  return async request => {
    requests.push(request);
    if (request.args.includes("status")) return {exitCode: 0, stderr: provider === "codex" ? "Logged in using ChatGPT" : "", stdout: provider === "claude" ? JSON.stringify({loggedIn: true, authMethod: "claude.ai", apiProvider: "firstParty", subscriptionType: "max"}) : ""};
    const stdout = outputs.shift(); if (stdout === undefined) throw new Error("Unexpected request");
    for (const line of stdout.split("\n")) request.onStdoutLine?.(line);
    return {stdout, stderr: "", exitCode: 0};
  };
}

describe("subscription CLI adapter", () => {
  test("drops API credentials, provider routing, environment injection, and nesting overrides", () => {
    const env = subscriptionEnvironment({HOME: "/home/test", PATH: "/bin", CODEX_HOME: "/private/auth", CLAUDE_CONFIG_DIR: "/private/claude", OPENAI_API_KEY: "secret", ANTHROPIC_AUTH_TOKEN: "secret", CLAUDE_CODE_OAUTH_TOKEN: "secret", ANTHROPIC_BASE_URL: "http://wrong", CLAUDE_CODE_USE_BEDROCK: "1", NODE_OPTIONS: "--require bad", BASH_ENV: "bad", CODEX_THREAD_ID: "parent"});
    expect(env.HOME).toBe("/home/test"); expect(env.CODEX_HOME).toBe("/private/auth");
    for (const key of ["OPENAI_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "NODE_OPTIONS", "BASH_ENV", "CODEX_THREAD_ID"]) expect(env[key]).toBeUndefined();
  });
  test("reuses both known Codex API-key status formats without exposing credentials or forcing ChatGPT", async () => {
    for (const status of ["Logged in using an API key: secret-sensitive-fragment", "API key configured - secret-sensitive-fragment\nAuthentication is checked when you send a request."]) {
      const requests: CliRequest[] = [];
      const cli: CliRunner = async request => {
        requests.push(request);
        return request.args.includes("status")
          ? {stdout: status, stderr: "", exitCode: 0}
          : {stdout: codexOutput(envelope({summary: "Checked"})), stderr: "", exitCode: 0};
      };
      const state = await probeSubscriptionLogin("codex", {runner: cli});
      expect(state.ready).toBe(true); expect(state.subscription).toBe(false);
      expect(state.authentication).toBe("api-key"); expect(state.message).toContain("API billing");
      expect(JSON.stringify(state)).not.toContain("secret-sensitive");
      expect((await new CodexClient("test", undefined, {runner: cli}).run(options())).summary).toBe("Checked");
      const scan = requests.find(request => !request.args.includes("status"))!;
      expect(scan.args).not.toContain('forced_login_method="chatgpt"');
      expect(scan.args).toContain("--ignore-user-config");
      expect(scan.env.OPENAI_API_KEY).toBeUndefined();
    }
  });
  test("unrecognized or unsuccessful Codex status does not authorize requests", async () => {
    for (const result of [{stdout: "Some other status", stderr: "", exitCode: 0}, {stdout: "API key configured - secret", stderr: "", exitCode: 1}]) {
      let calls = 0;
      const cli: CliRunner = async () => { calls++; return result; };
      expect((await probeSubscriptionLogin("codex", {runner: cli})).ready).toBe(false);
      await expect(new CodexClient("test", undefined, {runner: cli}).run(options())).rejects.toThrow("Codex");
      expect(calls).toBe(2);
    }
  });
  test("rejects Claude Console authentication even with a logged-in user", async () => {
    const cli: CliRunner = async () => ({stdout: JSON.stringify({loggedIn: true, authMethod: "api_key", apiProvider: "firstParty", subscriptionType: null}), stderr: "", exitCode: 0});
    expect((await probeSubscriptionLogin("claude", {runner: cli})).subscription).toBe(false);
  });
  test("Codex source requests pass only through the scanner callback and temporary files are removed", async () => {
    const requests: CliRequest[] = [], tools: unknown[] = [], usages: number[][] = [];
    const cli = runner([codexOutput(sourceEnvelope([{name: "read_file", args: {path: "app.ts", start: 1}}])), codexOutput(envelope({summary: "Checked"}))], "codex", requests);
    const client = new CodexClient("test-model", "high", {runner: cli});
    const result = await client.run(options({onTool(name, args) {tools.push({name,args}); return {source: "untrusted source"};}, onUsage(...tokens) {usages.push(tokens as number[]);}}));
    expect(result.summary).toBe("Checked"); expect(tools).toEqual([{name: "read_file", args: {path: "app.ts", start: 1}}]);
    const scans = requests.filter(r => !r.args.includes("status"));
    expect(scans).toHaveLength(2); expect(scans[1]!.input).toContain("untrusted source");
    expect(scans[0]!.args).toContain("--ignore-user-config"); expect(scans[0]!.args).toContain("forced_login_method=\"chatgpt\"");
    expect(scans[0]!.args).toContain("shell_tool"); expect(scans[0]!.cwd).toContain("openhack-subscription-");
    expect(await stat(scans[0]!.cwd).catch(() => null)).toBeNull(); expect(usages).toEqual([[13, 7, 2], [13, 7, 2]]);
  });
  test("Codex default selection defers model choice to the official CLI", async () => {
    const requests: CliRequest[] = [];
    await new CodexClient("default", undefined, {runner: runner([codexOutput(envelope({summary: "Checked"}))], "codex", requests)}).run(options());
    expect(requests.find(item => !item.args.includes("status"))!.args).not.toContain("--model");
  });
  test("subprocess cancellation terminates a resistant child group and waits for close", async () => {
    const directory = await mkdtemp(join(tmpdir(), "openhack-process-test-"));
    try {
      await writeFile(join(directory, "codex"), "#!/bin/sh\ntrap '' TERM\necho ready\n/bin/sleep 30\n", {mode: 0o700});
      const abort = new AbortController();
      const started = Date.now();
      const promise = runCli({command: "codex", args: [], cwd: directory, env: {...subscriptionEnvironment(), PATH: directory}, signal: abort.signal, onStdoutLine(line) {if (line === "ready") abort.abort();}});
      await expect(promise).rejects.toThrow("cancelled");
      expect(Date.now() - started).toBeLessThan(12000);
    } finally { await rm(directory, {recursive: true, force: true}); }
  }, 15000);
  test("Claude retains subscription auth and disables customization and native tools", async () => {
    const requests: CliRequest[] = [], usage: number[][] = [];
    let systemInstructions = "";
    const delegate = runner([claudeOutput(envelope({summary: "Checked"}))], "claude", requests);
    const cli: CliRunner = async request => {
      const promptIndex = request.args.indexOf("--system-prompt");
      if (promptIndex >= 0) systemInstructions = request.args[promptIndex + 1]!;
      return delegate(request);
    };
    const result = await new ClaudeCodeClient("sonnet", "high", {runner: cli}).run(options({onUsage(...tokens) {usage.push(tokens as number[]);}}));
    expect(result.summary).toBe("Checked");
    const request = requests.find(r => !r.args.includes("status"))!;
    expect(request.args).toContain("--safe-mode"); expect(request.args).not.toContain("--bare");
    expect(request.args).toContain("--system-prompt");
    expect(systemInstructions).toContain("The only native tool you may invoke is StructuredOutput");
    expect(systemInstructions).toContain("read_file, list_files, search, and graph_neighbors are NOT native tools");
    expect(systemInstructions).toContain("response.kind=tools and response.toolCalls containing the desired request");
    expect(systemInstructions).not.toContain("Do not use any native CLI tools.");
    expect(systemInstructions).toContain("In every round, submit your response with StructuredOutput.");
    expect(request.args[request.args.indexOf("--tools") + 1]).toBe("");
    expect(request.args).toContain("--strict-mcp-config"); expect(request.args).toContain("--no-session-persistence");
    expect(usage).toEqual([[14, 6, 2]]);
  });
  test("Codex nonfatal startup notices are not actions and never replace a completed result", () => {
    const notice = JSON.stringify({type: "item.completed", item: {id: "notice", type: "error", message: "Code Mode is unavailable because code-mode host is disabled."}});
    const answer = parseAnswer("codex", notice + "\n" + codexOutput(envelope({summary: "Checked"})), schema);
    expect(answer.envelope.result).toEqual({summary: "Checked"});
    expect(() => parseAnswer("codex", notice, schema)).toThrow("without completing");
    expect(() => parseAnswer("codex", notice + "\n" + JSON.stringify({type: "turn.failed"}), schema)).toThrow("could not complete");
  });
  test("native actions, hooks, and external tools fail closed", () => {
    expect(() => inspectEvent("codex", JSON.stringify({type: "item.started", item: {type: "command_execution", command: "ignored"}}))).toThrow("native tool");
    expect(() => inspectEvent("claude", JSON.stringify({type: "assistant", message: {content: [{type: "tool_use", name: "Bash"}]}}))).toThrow("native tool");
    expect(() => inspectEvent("claude", JSON.stringify({type: "system", subtype: "hook_started"}))).toThrow("hook");
    expect(() => inspectEvent("claude", JSON.stringify({type: "system", subtype: "init", tools: ["Read"]}))).toThrow("native tools");
  });
  test("invalid source calls never reach the scanner and invalid outputs remain incomplete", async () => {
    let called = false;
    const cli = runner([codexOutput(sourceEnvelope([{name: "read_file", args: {path: "app.ts", start: 0}}]))], "codex");
    await expect(new CodexClient("test", undefined, {runner: cli}).run(options({onTool() {called = true;}}))).rejects.toThrow("invalid scanner output");
    expect(called).toBe(false);
    expect(() => parseAnswer("codex", codexOutput({toolCalls: [], result: null}), schema)).toThrow("invalid scanner output");
    expect(() => parseAnswer("codex", '{"type":"turn.started"}', schema)).toThrow("without completing");
  });
  test("budget exhaustion cannot silently claim a complete pass", async () => {
    let finish: string | undefined;
    const cli = runner([codexOutput(sourceEnvelope([{name: "list_files", args: {contains: "", offset: 0}}])), codexOutput(envelope({summary: "Partial"}))], "codex");
    await new CodexClient("test", undefined, {runner: cli}).run(options({steps: 1, onFinish(value) {finish = value;}}));
    expect(finish).toBe("tool_limit");
  });
});


test("subscription login probing is cancellable and an already-cancelled pass starts no process", async () => {
  const abort = new AbortController(); let probes = 0;
  const cli: CliRunner = async request => {
    probes++;
    return new Promise((_resolve, reject) => {
      if (request.signal.aborted) return reject(request.signal.reason);
      request.signal.addEventListener("abort", () => reject(request.signal.reason), {once: true});
      abort.abort(new Error("cancel login probe"));
    });
  };
  const client = new CodexClient("default", undefined, {runner: cli});
  await expect(client.run(options({signal: abort.signal}))).rejects.toThrow("cancel login probe");
  expect(probes).toBe(1);
  await expect(client.run(options({signal: abort.signal}))).rejects.toThrow("cancel login probe");
  expect(probes).toBe(1);
});

test("subscription awaits source callbacks and never exposes thrown callback diagnostics", async () => {
  const calls = sourceEnvelope([{name: "read_file", args: {path: "app.ts", start: 1}}]);
  const requests: CliRequest[] = [];
  const cli = runner([codexOutput(calls), codexOutput(envelope({summary: "Checked"}))], "codex", requests);
  await new CodexClient("default", undefined, {runner: cli}).run(options({async onTool() {return {source: "awaited-source"};}}));
  expect(requests.at(-1)!.input).toContain('"result":{"source":"awaited-source"}');
  const failing = runner([codexOutput(calls)], "codex");
  const error = await new CodexClient("default", undefined, {runner: failing}).run(options({async onTool() {throw new Error("private-credential-value");}})).catch(error => error);
  expect(error.message).not.toContain("private-credential-value");
  expect(error.message).toContain("source tool could not complete");
});


test("cancellation also kills descendants that closed their inherited pipes", async () => {
  const directory = await mkdtemp(join(tmpdir(), "openhack-orphan-test-"));
  let pid: number | undefined;
  try {
    await writeFile(join(directory, "codex"), "#!/bin/sh\n/bin/sh -c 'trap \"\" TERM; exec /bin/sleep 30' </dev/null >/dev/null 2>&1 &\necho $! > child.pid\necho ready\n/bin/sleep 30\n", {mode: 0o700});
    const abort = new AbortController();
    await expect(runCli({command: "codex", args: [], cwd: directory, env: {...subscriptionEnvironment(), PATH: directory}, signal: abort.signal, onStdoutLine(line) {if (line === "ready") abort.abort();}})).rejects.toThrow("cancelled");
    pid = Number(await readFile(join(directory, "child.pid"), "utf8"));
    const alive = async () => {
      try { process.kill(pid!, 0); } catch { return false; }
      if (process.platform === "linux") {
        const state = await readFile(`/proc/${pid}/stat`, "utf8").catch(() => "");
        if (!state || /\) Z /.test(state)) return false;
      }
      return true;
    };
    for (let attempt = 0; attempt < 30 && await alive(); attempt++) await new Promise(resolve => setTimeout(resolve, 50));
    expect(await alive()).toBe(false);
  } finally {
    if (pid) try {process.kill(pid, "SIGKILL");} catch {}
    await rm(directory, {recursive: true, force: true});
  }
}, 15000);


type JsonSchemaNode = {type?: string; const?: unknown; items?: JsonSchemaNode; minimum?: number; maximum?: number; properties?: Record<string, JsonSchemaNode>; required?: string[]; anyOf?: JsonSchemaNode[]; additionalProperties?: boolean; maxLength?: number; description?: string};
const reconFixture = {overview: "A TypeScript service", stack: ["TypeScript"], entryPoints: ["app.ts"], trustBoundaries: ["HTTP request boundary"], risks: ["Check authorization"]};

test("both official CLIs receive the actual nested Recon schema and return its typed result", async () => {
  for (const provider of ["codex", "claude"] as const) {
    let sent: JsonSchemaNode | undefined;
    const delegate = runner([provider === "codex" ? codexOutput(envelope(reconFixture)) : claudeOutput(envelope(reconFixture))], provider);
    const cli: CliRunner = async request => {
      if (!request.args.includes("status")) {
        const json = provider === "codex"
          ? await readFile(request.args[request.args.indexOf("--output-schema") + 1]!, "utf8")
          : request.args[request.args.indexOf("--json-schema") + 1]!;
        sent = JSON.parse(json) as JsonSchemaNode;
        expect(request.input).toContain('"response":{"kind":"tools"');
        expect(request.input).not.toContain("resultJson");
      }
      return delegate(request);
    };
    const client = provider === "codex" ? new CodexClient("default", undefined, {runner: cli}) : new ClaudeCodeClient("sonnet", undefined, {runner: cli});
    const result = await client.run({...options(), name: "Recon", schema: reconSchema});
    expect(result).toEqual(reconFixture);
    expect(sent!.required).toEqual(["response"]);
    const responses = sent!.properties!.response!.anyOf!;
    expect(responses).toHaveLength(2);
    const toolsResponse = responses.find(node => node.properties?.kind?.const === "tools")!;
    const resultResponse = responses.find(node => node.properties?.kind?.const === "result")!;
    expect(toolsResponse.required).toEqual(["kind", "toolCalls"]);
    expect(resultResponse.required).toEqual(["kind", "result"]);
    const toolVariants = toolsResponse.properties!.toolCalls!.items!.anyOf!;
    expect(toolVariants).toHaveLength(4);
    const readCall = toolVariants.find(node => node.properties?.name?.const === "read_file")!;
    expect(readCall.required).toEqual(["name", "args"]);
    expect(readCall.properties!.args!.required).toEqual(["path", "start"]);
    expect(readCall.properties!.args!.additionalProperties).toBe(false);
    expect(readCall.properties!.args!.properties!.start!.type).toBe("integer");
    expect(JSON.stringify(toolVariants)).not.toContain("argsJson");
    if (provider === "codex") expect(readCall.properties!.args!.properties!.start!.minimum).toBe(1);
    const nested = resultResponse.properties!.result!;
    expect(nested.required).toEqual(["overview", "stack", "entryPoints", "trustBoundaries", "risks"]);
    expect(nested.additionalProperties).toBe(false);
    expect(nested.properties!.stack!.type).toBe("array");
    if (provider === "claude") {
      expect(nested.properties!.overview!.maxLength).toBeUndefined();
      expect(nested.properties!.overview!.description).toContain("maxLength: 6000");
    } else expect(nested.properties!.overview!.maxLength).toBe(6000);
  }
});

test("malformed Recon results and legacy JSON strings fail without coercion or success status", async () => {
  const invalid = [
    envelope(JSON.stringify(reconFixture)),
    envelope({overview: "Missing required fields"}),
    envelope({...reconFixture, stack: "TypeScript"}),
    {toolCalls: [], resultJson: JSON.stringify(reconFixture)},
  ];
  for (const value of invalid) {
    let finish: string | undefined; const usage: number[][] = [];
    const client = new ClaudeCodeClient("sonnet", undefined, {runner: runner([claudeOutput(value)], "claude")});
    await expect(client.run({...options(), name: "Recon", schema: reconSchema, onFinish(reason) {finish = reason;}, onUsage(...tokens) {usage.push(tokens as number[]);}})).rejects.toThrow("invalid scanner output");
    expect(finish).toBeUndefined();
    expect(usage).toEqual([[14, 6, 2]]);
  }
});

test("local task validation retains bounds omitted from Claude's compatible schema", async () => {
  const taskSchema = z.object({verdict: z.enum(["validated", "rejected"]), line: z.number().int().min(1), evidence: z.string().min(3).max(8)});
  for (const result of [{verdict: "validated", line: 0, evidence: "source"}, {verdict: "validated", line: "1", evidence: "source"}, {verdict: "maybe", line: 1, evidence: "source"}, {verdict: "validated", line: 1, evidence: "overly long evidence"}]) {
    const client = new ClaudeCodeClient("sonnet", undefined, {runner: runner([claudeOutput(envelope(result))], "claude")});
    await expect(client.run({...options(), schema: taskSchema})).rejects.toThrow("invalid scanner output");
  }
  expect(() => parseAnswer("claude", claudeOutput({toolCalls: [{name: "read_file", args: {path: "app.ts", start: 1}}], result: reconFixture}), reconSchema)).toThrow("invalid scanner output");
});


test("task validation diagnostics contain schema paths and codes without provider values", () => {
  const privateValue = "private-provider-payload";
  try {
    parseAnswer("claude", claudeOutput(envelope({...reconFixture, stack: privateValue})), reconSchema);
    throw new Error("Expected schema rejection");
  } catch (error) {
    expect(String(error)).toContain("result.stack: invalid_type");
    expect(String(error)).not.toContain(privateValue);
  }
  const bounded = z.object({line: z.number().int().min(1)});
  expect(() => parseAnswer("codex", codexOutput(envelope({line: 0})), bounded)).toThrow("result.line: too_small");
});


test("typed source requests reject missing fields, invalid lines, unsafe integers, and legacy strings before callbacks", async () => {
  const invalidCalls = [
    {name: "read_file", args: {path: "app.ts"}},
    {name: "read_file", args: {path: "app.ts", start: 0}},
    {name: "read_file", args: {path: "app.ts", start: "1"}},
    {name: "read_file", args: {path: "app.ts", start: Number.MAX_SAFE_INTEGER + 1}},
    {name: "read_file", args: {path: "app.ts", start: 1, command: "not allowed"}},
    {name: "list_files", args: {contains: "", offset: -1}},
    {name: "graph_neighbors", args: {path: "app.ts"}},
    {name: "search", args: {query: "lookup"}},
    {name: "search", args: {query: "a".repeat(12001), pathContains: ""}},
    {name: "read_file", argsJson: '{"path":"app.ts","start":1}'},
    {name: "read_file", args: '{"path":"app.ts","start":1}'},
  ];
  for (const call of invalidCalls) {
    let executions = 0;
    const client = new ClaudeCodeClient("sonnet", undefined, {runner: runner([claudeOutput(sourceEnvelope([call]))], "claude")});
    await expect(client.run(options({onTool() {executions++;}}))).rejects.toThrow("invalid scanner output");
    expect(executions).toBe(0);
  }
  expect(() => parseAnswer("claude", claudeOutput(sourceEnvelope([{name: "read_file", args: {path: "app.ts", start: 0}}])), schema)).toThrow("args.start: too_small");
});

test("shared source argument validation preserves direct API signatures and all four typed variants", () => {
  const cases: {name: ScanToolName; args: ScanToolCall["args"]; types: Record<string, {type: "string" | "integer"}>}[] = [
    {name: "graph_neighbors", args: {path: "app.ts", offset: 0}, types: {path: {type: "string"}, offset: {type: "integer"}}},
    {name: "list_files", args: {contains: "", offset: 0}, types: {contains: {type: "string"}, offset: {type: "integer"}}},
    {name: "read_file", args: {path: "app.ts", start: 1}, types: {path: {type: "string"}, start: {type: "integer"}}},
    {name: "search", args: {query: "lookup", pathContains: ""}, types: {query: {type: "string"}, pathContains: {type: "string"}}},
  ];
  for (const item of cases) {
    expect(parseScanToolArguments(item.name, item.args)).toEqual(item.args);
    const parsed = parseAnswer("codex", codexOutput(sourceEnvelope([{name: item.name, args: item.args}])), schema);
    expect(parsed.envelope.toolCalls[0]!.args).toEqual(item.args);
    expect(scanTools.find(tool => tool.name === item.name)!.parameters).toEqual({type: "object", properties: item.types, required: Object.keys(item.types), additionalProperties: false});
  }
});


const isolatedClaudeInit = JSON.stringify({type: "system", subtype: "init", tools: ["StructuredOutput"], mcp_servers: []});
const nativeSourceCall = {type: "tool_use", id: "source-1", name: "read_file", input: {path: "app.ts", start: 1}};
const assistantTools = (calls: unknown[]) => JSON.stringify({type: "assistant", message: {content: calls}});

test("Claude source interception requires verified isolation and validates the whole assistant batch", () => {
  const invalidSequences = [
    [assistantTools([nativeSourceCall])],
    [JSON.stringify({type: "system", subtype: "init", tools: ["StructuredOutput", "Read"], mcp_servers: []})],
    [JSON.stringify({type: "system", subtype: "init", tools: ["StructuredOutput"], mcp_servers: [{name: "external"}]})],
    [JSON.stringify({type: "system", subtype: "init", tools: ["StructuredOutput"]})],
    [isolatedClaudeInit, assistantTools([nativeSourceCall, {type: "tool_use", id: "other", name: "StructuredOutput", input: envelope({summary: "ignored"})}])],
    [isolatedClaudeInit, assistantTools([nativeSourceCall, {type: "tool_use", id: "other", name: "Bash", input: {command: "never"}}])],
    [isolatedClaudeInit, assistantTools([{...nativeSourceCall, input: {path: "app.ts", start: 0}}])],
    [isolatedClaudeInit, assistantTools([{...nativeSourceCall, input: {path: "app.ts"}}])],
    [isolatedClaudeInit, assistantTools(Array.from({length: 9}, (_, index) => ({...nativeSourceCall, id: String(index)})))],
  ];
  for (const sequence of invalidSequences) {
    const observe = createClaudeProtocolMonitor(); let failure: unknown;
    try {for (const line of sequence) observe(line);} catch (error) {failure = error;}
    expect(failure).toBeInstanceOf(Error); expect(failure).not.toBeInstanceOf(ClaudeSourceRequest);
  }
  const observe = createClaudeProtocolMonitor(); observe(isolatedClaudeInit);
  let intercepted: unknown;
  try {observe(assistantTools([nativeSourceCall, {type: "tool_use", id: "source-2", name: "list_files", input: {contains: "", offset: 0}}]));} catch (error) {intercepted = error;}
  expect(intercepted).toBeInstanceOf(ClaudeSourceRequest);
  expect((intercepted as ClaudeSourceRequest).calls).toEqual([{name: "read_file", args: {path: "app.ts", start: 1}}, {name: "list_files", args: {contains: "", offset: 0}}]);
  expect(() => inspectEvent("claude", assistantTools([nativeSourceCall]))).toThrow("native tool action");
});

test("intercepted source requests execute only after runner exit and preserve known usage accounting", async () => {
  let invocation = 0, exited = false, callbacks = 0; const usage: number[][] = [], events: string[] = [];
  const cli: CliRunner = async request => {
    if (request.args.includes("status")) return {stdout: JSON.stringify({loggedIn: true, authMethod: "claude.ai", apiProvider: "firstParty", subscriptionType: "max"}), stderr: "", exitCode: 0};
    invocation++;
    if (invocation === 1) {
      try {request.onStdoutLine!(isolatedClaudeInit); request.onStdoutLine!(assistantTools([nativeSourceCall]));}
      finally {exited = true;}
      throw new Error("Expected interception");
    }
    expect(exited).toBe(true); expect(callbacks).toBe(1);
    expect(request.input).toContain('"result":{"source":"snapshot-only"}');
    expect(request.input).toContain('"response":{"kind":"tools"');
    const stdout = claudeOutput(envelope({summary: "Checked"}));
    for (const line of stdout.split("\n")) request.onStdoutLine!(line);
    return {stdout, stderr: "", exitCode: 0};
  };
  const result = await new ClaudeCodeClient("sonnet", undefined, {runner: cli}).run(options({onTool(name, args) {expect(exited).toBe(true); expect(name).toBe("read_file"); expect(args).toEqual({path: "app.ts", start: 1}); callbacks++; return {source: "snapshot-only"};}, onUsage(...tokens) {usage.push(tokens as number[]);}, onEvent(event) {events.push(event);}}));
  expect(result.summary).toBe("Checked"); expect(invocation).toBe(2);
  expect(usage).toEqual([[14, 6, 2]]);
  expect(events.some(event => event.includes("Token usage for this interrupted turn is unavailable"))).toBe(true);
});

test("interception cannot execute source operations beyond the ordinary tool-round budget", async () => {
  let executions = 0, invocations = 0, finish = "";
  const cli: CliRunner = async request => {
    if (request.args.includes("status")) return {stdout: JSON.stringify({loggedIn: true, authMethod: "claude.ai", apiProvider: "firstParty", subscriptionType: "max"}), stderr: "", exitCode: 0};
    invocations++; request.onStdoutLine!(isolatedClaudeInit); request.onStdoutLine!(assistantTools([nativeSourceCall]));
    throw new Error("Expected interception");
  };
  await expect(new ClaudeCodeClient("sonnet", undefined, {runner: cli}).run(options({steps: 0, onTool() {executions++;}, onFinish(reason) {finish = reason;}}))).rejects.toThrow("tool budget");
  expect(executions).toBe(0); expect(invocations).toBe(1); expect(finish).toBe("tool_limit");
});

test("real subprocess termination preserves the typed source request for safe post-exit handling", async () => {
  const directory = await mkdtemp(join(tmpdir(), "openhack-interception-test-"));
  try {
    const line = assistantTools([nativeSourceCall]);
    await writeFile(join(directory, "claude"), `#!/bin/sh\nprintf '%s\\n' '${isolatedClaudeInit}' '${line}'\n/bin/sleep 30\n`, {mode: 0o700});
    const failure = await runCli({command: "claude", args: [], cwd: directory, env: {...subscriptionEnvironment(), PATH: directory}, signal: new AbortController().signal, onStdoutLine: createClaudeProtocolMonitor()}).catch(error => error);
    expect(failure).toBeInstanceOf(ClaudeSourceRequest);
    expect((failure as ClaudeSourceRequest).calls).toEqual([{name: "read_file", args: {path: "app.ts", start: 1}}]);
  } finally {await rm(directory, {recursive: true, force: true});}
}, 15000);

test("wire response schema rejects both/neither branches and empty source requests", () => {
  const invalid = [
    {response: {kind: "tools", toolCalls: []}},
    {response: {kind: "tools", toolCalls: [{name: "read_file", args: {path: "app.ts", start: 1}}], result: {summary: "mixed"}}},
    {response: {kind: "result", result: {summary: "mixed"}, toolCalls: []}},
    {response: {kind: "result", result: null}},
    {response: {kind: "result"}},
    {response: {}},
  ];
  for (const value of invalid) expect(() => parseAnswer("claude", claudeOutput(value), schema)).toThrow("invalid scanner output");
});


test("source interception also handles a final CLI event without a trailing newline", async () => {
  const directory = await mkdtemp(join(tmpdir(), "openhack-tail-event-test-"));
  try {
    const line = assistantTools([nativeSourceCall]);
    await writeFile(join(directory, "claude"), `#!/bin/sh\nprintf '%s\\n' '${isolatedClaudeInit}'\nprintf '%s' '${line}'\n`, {mode: 0o700});
    const failure = await runCli({command: "claude", args: [], cwd: directory, env: {...subscriptionEnvironment(), PATH: directory}, signal: new AbortController().signal, onStdoutLine: createClaudeProtocolMonitor()}).catch(error => error);
    expect(failure).toBeInstanceOf(ClaudeSourceRequest);
  } finally {await rm(directory, {recursive: true, force: true});}
}, 15000);


test("a later conflicting buffered event invalidates an earlier source interception", async () => {
  for (const tail of [assistantTools([{type: "tool_use", id: "bad", name: "Bash", input: {command: "never"}}]), assistantTools([{type: "tool_use", id: "structured", name: "StructuredOutput", input: envelope({summary: "mixed"})}])]) {
    let executions = 0;
    const cli: CliRunner = async request => {
      if (request.args.includes("status")) return {stdout: JSON.stringify({loggedIn: true, authMethod: "claude.ai", apiProvider: "firstParty", subscriptionType: "max"}), stderr: "", exitCode: 0};
      request.onStdoutLine!(isolatedClaudeInit);
      let first: unknown;
      try {request.onStdoutLine!(assistantTools([nativeSourceCall]));} catch (error) {first = error;}
      expect(first).toBeInstanceOf(ClaudeSourceRequest);
      try {request.onStdoutLine!(tail);} catch {}
      expect((first as ClaudeSourceRequest).recoverable).toBe(false);
      throw first;
    };
    await expect(new ClaudeCodeClient("sonnet", undefined, {runner: cli}).run(options({onTool() {executions++;}}))).rejects.toThrow("conflicting native tool events");
    expect(executions).toBe(0);
  }
});

test("real buffered tail without newline cannot conceal a conflicting native call", async () => {
  const directory = await mkdtemp(join(tmpdir(), "openhack-conflicting-tail-test-"));
  try {
    const source = assistantTools([nativeSourceCall]);
    const forbidden = assistantTools([{type: "tool_use", id: "bad", name: "Bash", input: {command: "never"}}]);
    await writeFile(join(directory, "claude"), `#!/bin/sh\nprintf '%s\\n%s\\n%s' '${isolatedClaudeInit}' '${source}' '${forbidden}'\n`, {mode: 0o700});
    const failure = await runCli({command: "claude", args: [], cwd: directory, env: {...subscriptionEnvironment(), PATH: directory}, signal: new AbortController().signal, onStdoutLine: createClaudeProtocolMonitor()}).catch(error => error);
    expect(failure).toBeInstanceOf(ClaudeSourceRequest);
    expect((failure as ClaudeSourceRequest).recoverable).toBe(false);
  } finally {await rm(directory, {recursive: true, force: true});}
}, 15000);

const invalidKnownCall = {...nativeSourceCall, input: {path: "private-source-value", start: 0, "private-field-name": "private-field-value"}};
const invalidKnownOutput = [isolatedClaudeInit, assistantTools([invalidKnownCall])].join("\n");

test("known invalid source batches produce value-free diagnostics and never expose unknown names", () => {
  const observe = createClaudeProtocolMonitor(); observe(isolatedClaudeInit);
  let failure: unknown;
  try {observe(assistantTools([nativeSourceCall, {...invalidKnownCall, id: "invalid"}]));} catch (error) {failure = error;}
  expect(failure).toBeInstanceOf(ClaudeSourceCorrection);
  const correction = failure as ClaudeSourceCorrection;
  expect(correction.diagnostics).toContain("toolCalls[1].read_file.args.start: too_small");
  expect(correction.diagnostics).toContain("unrecognized_keys");
  expect(JSON.stringify(correction)).not.toContain("private-");
  expect(correction.message).not.toContain("private-");
  for (const calls of [[invalidKnownCall, {type: "tool_use", id: "unknown", name: "private-native-name", input: {}}], [invalidKnownCall, {type: "tool_use", id: "structured", name: "StructuredOutput", input: {}}]]) {
    const monitor = createClaudeProtocolMonitor(); monitor(isolatedClaudeInit);
    let rejected: unknown;
    try {monitor(assistantTools(calls));} catch (error) {rejected = error;}
    expect(rejected).toBeInstanceOf(Error); expect(rejected).not.toBeInstanceOf(ClaudeSourceCorrection);
    expect(String(rejected)).not.toContain("private-");
  }
});

test("known argument correction waits for runner exit, rejects the entire batch, and can finish", async () => {
  const requests: CliRequest[] = [], events: string[] = [], usage: number[][] = [];
  let exited = false, callbacks = 0;
  const base = runner([
    [isolatedClaudeInit, assistantTools([nativeSourceCall, {...invalidKnownCall, id: "invalid"}])].join("\n"),
    claudeOutput(sourceEnvelope([{name: "read_file", args: {path: "app.ts", start: 1}}])),
    claudeOutput(envelope({summary: "Checked"})),
  ], "claude", requests);
  const cli: CliRunner = async request => {
    if (!request.args.includes("status") && requests.filter(item => !item.args.includes("status")).length > 0) expect(exited).toBe(true);
    try {return await base(request);} finally {if (!request.args.includes("status")) exited = true;}
  };
  const result = await new ClaudeCodeClient("sonnet", undefined, {runner: cli}).run(options({onEvent(event) {events.push(event);}, onUsage(...tokens) {usage.push(tokens as number[]);}, onTool(name, args) {expect(exited).toBe(true); expect(name).toBe("read_file"); expect(args).toEqual({path: "app.ts", start: 1}); callbacks++; return "source";}}));
  expect(result.summary).toBe("Checked"); expect(callbacks).toBe(1);
  const passes = requests.filter(request => !request.args.includes("status"));
  expect(passes).toHaveLength(3); expect(passes[1]!.input).toContain("no operations were executed");
  expect(passes[1]!.input).toContain("read_file.args.start: too_small");
  expect(passes[1]!.input).not.toContain("private-");
  expect(events.join("\n")).not.toContain("private-");
  expect(events.some(event => event.includes("Token usage for this interrupted turn is unavailable"))).toBe(true);
  expect(usage).toEqual([[14, 6, 2], [14, 6, 2]]);
});

test("at most two known-argument corrections are allowed within a pass", async () => {
  const requests: CliRequest[] = []; let callbacks = 0;
  const cli = runner(Array(4).fill(invalidKnownOutput), "claude", requests);
  await expect(new ClaudeCodeClient("sonnet", undefined, {runner: cli}).run(options({steps: 10, onTool() {callbacks++;}}))).rejects.toThrow("correction limit");
  expect(requests.filter(request => !request.args.includes("status"))).toHaveLength(3);
  expect(callbacks).toBe(0);
});

test("known-argument correction consumes the existing budget and cannot retry the final round", async () => {
  for (const steps of [0, 1]) {
    const requests: CliRequest[] = []; let callbacks = 0, finish = "";
    const cli = runner(Array(3).fill(invalidKnownOutput), "claude", requests);
    await expect(new ClaudeCodeClient("sonnet", undefined, {runner: cli}).run(options({steps, onTool() {callbacks++;}, onFinish(reason) {finish = reason;}}))).rejects.toThrow("source-tool budget");
    expect(requests.filter(request => !request.args.includes("status"))).toHaveLength(steps + 1);
    expect(callbacks).toBe(0); expect(finish).toBe("tool_limit");
  }
});

test("cancellation while closing an invalid-source turn prevents correction requests", async () => {
  const controller = new AbortController(), requests: CliRequest[] = []; let callbacks = 0;
  const base = runner([invalidKnownOutput], "claude", requests);
  const cli: CliRunner = async request => {
    try {return await base(request);} finally {if (!request.args.includes("status")) controller.abort();}
  };
  await expect(new ClaudeCodeClient("sonnet", undefined, {runner: cli}).run(options({signal: controller.signal, onTool() {callbacks++;}}))).rejects.toThrow();
  expect(requests.filter(request => !request.args.includes("status"))).toHaveLength(1); expect(callbacks).toBe(0);
});

test("conflicting buffered events invalidate argument corrections before retry", async () => {
  for (const tail of [assistantTools([{type: "tool_use", id: "bad", name: "Bash", input: {command: "never"}}]), assistantTools([nativeSourceCall]), JSON.stringify({type: "result", subtype: "success"})]) {
    let invocations = 0, callbacks = 0;
    const auth = runner([], "claude");
    const cli: CliRunner = async request => {
      if (request.args.includes("status")) return auth(request);
      invocations++; request.onStdoutLine!(isolatedClaudeInit);
      let first: unknown;
      try {request.onStdoutLine!(assistantTools([invalidKnownCall]));} catch (error) {first = error;}
      expect(first).toBeInstanceOf(ClaudeSourceCorrection);
      try {request.onStdoutLine!(tail);} catch {}
      expect((first as ClaudeSourceCorrection).recoverable).toBe(false);
      throw first;
    };
    await expect(new ClaudeCodeClient("sonnet", undefined, {runner: cli}).run(options({onTool() {callbacks++;}}))).rejects.toThrow("conflicting native tool events");
    expect(invocations).toBe(1); expect(callbacks).toBe(0);
  }
});

test("real non-newline conflicting tail invalidates known-argument correction", async () => {
  const directory = await mkdtemp(join(tmpdir(), "openhack-correction-tail-"));
  try {
    const source = assistantTools([invalidKnownCall]);
    const forbidden = assistantTools([{type: "tool_use", id: "bad", name: "Bash", input: {command: "never"}}]);
    await writeFile(join(directory, "claude"), `#!/bin/sh\nprintf '%s\\n%s\\n%s' '${isolatedClaudeInit}' '${source}' '${forbidden}'\n`, {mode: 0o700});
    const failure = await runCli({command: "claude", args: [], cwd: directory, env: {...subscriptionEnvironment(), PATH: directory}, signal: new AbortController().signal, onStdoutLine: createClaudeProtocolMonitor()}).catch(error => error);
    expect(failure).toBeInstanceOf(ClaudeSourceCorrection); expect((failure as ClaudeSourceCorrection).recoverable).toBe(false);
  } finally {await rm(directory, {recursive: true, force: true});}
}, 15000);
