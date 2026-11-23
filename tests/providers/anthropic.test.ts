import { describe, expect, test } from "bun:test";
import { z } from "zod";
import { AnthropicClient, anthropicOutputSchema } from "../../src/providers/anthropic";
import type { AgentOptions } from "../../src/harness/openai";
import { scanTools } from "../../src/harness/tools";

const schema = z.object({ summary: z.string().min(3).max(20), count: z.number().int().min(1), tags: z.array(z.string()).max(2) });
type Result = z.infer<typeof schema>;
const result: Result = { summary: "Reviewed", count: 1, tags: [] };
const text = [{ type: "text", text: JSON.stringify(result) }];
function response(content: unknown = text, stop_reason = "end_turn", extra: Record<string, unknown> = {}) {
  return Response.json({ content, stop_reason, usage: { input_tokens: 10, output_tokens: 5, cache_read_input_tokens: 3, cache_creation_input_tokens: 2 }, ...extra });
}
function options(overrides: Partial<AgentOptions<Result>> = {}): AgentOptions<Result> {
  return { name: "Review", prompt: "Review fixture", schema, steps: 3, signal: new AbortController().signal, onEvent() {}, onUsage() {}, onTool() { return "fixture source"; }, ...overrides };
}

describe("Anthropic Messages adapter", () => {
  test("sends strict tool and output schemas, preserves thinking blocks, and accounts for cached usage", async () => {
    const requests: { url: string; init: RequestInit; body: Record<string, any> }[] = [];
    const toolCalls: { name: string; args: unknown }[] = [];
    const usage: number[][] = [], finishes: string[] = [];
    let gates = 0;
    const firstContent = [
      { type: "thinking", thinking: "Fixture thought", signature: "signed-value" },
      { type: "tool_use", id: "tool-1", name: "read_file", input: { path: "app.ts", start: 1 } },
    ];
    const client = new AnthropicClient("test-key", "claude-sonnet-4-6", async (url, init) => {
      requests.push({ url, init, body: JSON.parse(String(init.body)) });
      return requests.length === 1 ? response(firstContent, "tool_use") : response();
    }, "high");
    expect(await client.run(options({
      beforeRequest: async () => { gates++; },
      onTool(name, args) { toolCalls.push({ name, args }); return "fixture source"; },
      onUsage: (input, output, cached = 0) => { usage.push([input, output, cached]); },
      onFinish: reason => { finishes.push(reason); },
    }))).toEqual(result);
    expect(requests[0]!.url).toBe("https://api.anthropic.com/v1/messages");
    expect(new Headers(requests[0]!.init.headers).get("x-api-key")).toBe("test-key");
    expect(new Headers(requests[0]!.init.headers).get("anthropic-version")).toBe("2023-06-01");
    expect(requests[0]!.body.output_config.format.type).toBe("json_schema");
    expect(requests[0]!.body.output_config.effort).toBe("high");
    expect(requests[0]!.body.tools.map((tool: { name: string }) => tool.name)).toEqual(scanTools.map(tool => tool.name));
    expect(requests[0]!.body.tools.every((tool: { strict: boolean }) => tool.strict)).toBe(true);
    expect(requests[0]!.body.tool_choice).toEqual({ type: "auto", disable_parallel_tool_use: true });
    expect(requests[1]!.body.messages[1]).toEqual({ role: "assistant", content: firstContent });
    expect(requests[1]!.body.messages[2].content).toEqual([{ type: "tool_result", tool_use_id: "tool-1", content: JSON.stringify("fixture source") }]);
    expect(toolCalls).toEqual([{ name: "read_file", args: { path: "app.ts", start: 1 } }]);
    expect(usage).toEqual([[15, 5, 3], [15, 5, 3]]);
    expect(gates).toBe(2);
    expect(finishes).toEqual(["completed"]);
  });

  test("simplifies provider-unsupported constraints but validates the original schema", async () => {
    const converted = anthropicOutputSchema(z.toJSONSchema(schema)) as Record<string, any>;
    expect(converted.properties.summary.minLength).toBeUndefined();
    expect(converted.properties.summary.description).toContain("minLength: 3");
    expect(converted.properties.count.minimum).toBeUndefined();
    expect(converted.properties.tags.maxItems).toBeUndefined();
    expect(converted.additionalProperties).toBe(false);
    const client = new AnthropicClient("test", "test", async () => response([{ type: "text", text: JSON.stringify({ ...result, count: 0 }) }]));
    await expect(client.run(options())).rejects.toThrow("invalid structured output");
  });

  test("enforces final tool-free round and reports tool budget exhaustion", async () => {
    const bodies: Record<string, any>[] = [], finishes: string[] = [];
    const client = new AnthropicClient("test", "test", async (_url, init) => {
      bodies.push(JSON.parse(String(init.body)));
      return bodies.length === 1 ? response([{ type: "tool_use", id: "tool-1", name: "list_files", input: { contains: "", offset: 0 } }], "tool_use") : response();
    });
    await client.run(options({ steps: 1, onFinish: reason => { finishes.push(reason); } }));
    expect(bodies).toHaveLength(2);
    expect(bodies[1]!.tool_choice).toEqual({ type: "none" });
    expect(finishes).toEqual(["tool_limit"]);
    let calls = 0;
    const overBudget = new AnthropicClient("test", "test", async () => response([{ type: "tool_use", id: "tool-2", name: "read_file", input: {} }], "tool_use"));
    await expect(overBudget.run(options({ steps: 0, onTool() { calls++; } }))).rejects.toThrow("tool budget");
    expect(calls).toBe(0);
  });

  test("rejects truncated/refused/malformed responses without exposing response data", async () => {
    for (const reason of ["max_tokens", "pause_turn", "refusal", "unknown-secret-stop"]) {
      const client = new AnthropicClient("test", "test", async () => response(text, reason));
      const error = await client.run(options()).catch(error => error as Error);
      expect(error).toBeInstanceOf(Error);
      expect((error as Error).message).not.toContain("unknown-secret-stop");
    }
    const refused = new AnthropicClient("test", "test", async () => response(text, "end_turn", { stop_details: { type: "refusal", explanation: "private input" } }));
    await expect(refused.run(options())).rejects.toThrow("declined");
    const malformed = new AnthropicClient("test", "test", async () => Response.json({ private: "sensitive source" }));
    const error = await malformed.run(options()).catch(error => error as Error);
    expect((error as Error).message).toContain("invalid response");
    expect((error as Error).message).not.toContain("sensitive source");
  });

  test("rejects unknown tool execution and sends a sanitized tool error", async () => {
    let requests = 0, executions = 0;
    const client = new AnthropicClient("test", "test", async (_url, init) => {
      if (++requests === 1) return response([{ type: "tool_use", id: "tool-1", name: "shell", input: { command: "do not execute" } }], "tool_use");
      const body = JSON.parse(String(init.body));
      expect(body.messages[2].content[0].is_error).toBe(true);
      return response();
    });
    await client.run(options({ onTool() { executions++; } }));
    expect(executions).toBe(0);
  });

  test("cancels requests and retry waits without another provider call", async () => {
    const controller = new AbortController();
    let requests = 0;
    const client = new AnthropicClient("test", "test", async (_url, init) => {
      requests++;
      return await new Promise<Response>((_resolve, reject) => {
        init.signal!.addEventListener("abort", () => reject(init.signal!.reason), { once: true });
        controller.abort(new Error("cancelled fixture"));
      });
    });
    await expect(client.run(options({ signal: controller.signal }))).rejects.toThrow("cancelled fixture");
    expect(requests).toBe(1);
    const retryController = new AbortController();
    const retrying = new AnthropicClient("test", "test", async () => { requests++; return new Response("private error", { status: 429 }); });
    await expect(retrying.run(options({ signal: retryController.signal, onEvent(event) { if (event.includes("retry")) retryController.abort(new Error("stop retries")); } }))).rejects.toThrow("stop retries");
    expect(requests).toBe(2);
  });

  test("retries transient HTTP failures and sanitizes terminal failures", async () => {
    let requests = 0;
    const events: string[] = [];
    const client = new AnthropicClient("test", "test", async () => ++requests === 1 ? new Response("private error", { status: 529, headers: { "retry-after": "1" } }) : response());
    expect(await client.run(options({ onEvent: event => { events.push(event); } }))).toEqual(result);
    expect(requests).toBe(2);
    expect(events.some(event => event.includes("retry 2/3"))).toBe(true);
    const denied = new AnthropicClient("test", "test", async () => new Response("SECRET credential echoed", { status: 401 }));
    const error = await denied.run(options()).catch(error => error as Error);
    expect((error as Error).message).toContain("ANTHROPIC_API_KEY");
    expect((error as Error).message).not.toContain("SECRET");
  });

  test("rejects missing keys and unsupported effort names without a request", () => {
    expect(() => new AnthropicClient("", "test")).toThrow("ANTHROPIC_API_KEY");
    expect(() => new AnthropicClient("test", "test", undefined, "minimal")).toThrow("reasoning must");
    expect(() => new AnthropicClient("test", "test", undefined, "none")).toThrow("reasoning must");
  });
});


test("Anthropic validates source arguments and stops batched tools when cancelled", async () => {
  let requests = 0, executions = 0;
  const invalid = new AnthropicClient("test", "test", async () => ++requests === 1
    ? response([{type: "tool_use", id: "bad", name: "read_file", input: {path: "app.ts", start: 0}}], "tool_use") : response());
  await invalid.run(options({onTool() {executions++;}}));
  expect(executions).toBe(0);
  const abort = new AbortController();
  const client = new AnthropicClient("test", "test", async () => response([
    {type: "tool_use", id: "one", name: "read_file", input: {path: "app.ts", start: 1}},
    {type: "tool_use", id: "two", name: "read_file", input: {path: "app.ts", start: 2}},
  ], "tool_use"));
  await expect(client.run(options({signal: abort.signal, onTool() {executions++; abort.abort(new Error("stop tools")); return "source";}}))).rejects.toThrow("stop tools");
  expect(executions).toBe(1);
});
