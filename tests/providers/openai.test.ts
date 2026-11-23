import { describe, expect, test } from "bun:test";
import { z } from "zod";
import { OpenAIClient, type AgentOptions } from "../../src/harness/openai";

const schema = z.object({ok: z.boolean()});
function options(patch: Partial<AgentOptions<z.infer<typeof schema>>> = {}): AgentOptions<z.infer<typeof schema>> {
  return {name: "Review", prompt: "fixture", schema, steps: 3, signal: new AbortController().signal, onUsage() {}, onEvent() {}, onTool() {return "fixture";}, ...patch};
}
const text = [{type: "message", content: [{type: "output_text", text: '{"ok":true}'}]}];
const call = (name: string, args: unknown, id = "call-1") => ({type: "function_call", name, arguments: JSON.stringify(args), call_id: id});
const response = (output: unknown = text, status = "completed") => Response.json({status, output});

describe("OpenAI source-only boundary", () => {
  test("does not dispatch unknown tools or malformed arguments", async () => {
    for (const tool of [call("shell", {command: "not allowed"}), call("read_file", {path: "app.ts", start: 0}), call("read_file", {path: "app.ts", start: 1, extra: "unexpected"})]) {
      let executions = 0, requests = 0;
      const client = new OpenAIClient("fixture", "fixture", undefined, async (_url, init) => {
        if (++requests === 1) return response([tool]);
        expect(JSON.parse(String(init.body)).input.at(-1).output).toContain("Invalid tool arguments");
        return response();
      });
      expect(await client.run(options({onTool() {executions++;}}))).toEqual({ok: true});
      expect(executions).toBe(0);
    }
  });
  test("awaits asynchronous source callbacks before the next model request", async () => {
    let requests = 0;
    const client = new OpenAIClient("fixture", "fixture", undefined, async (_url, init) => {
      if (++requests === 1) return response([call("read_file", {path: "app.ts", start: 1})]);
      expect(JSON.parse(String(init.body)).input.at(-1).output).toBe('{"source":"awaited"}');
      return response();
    });
    await client.run(options({async onTool() {return {source: "awaited"};}}));
  });
  test("stops a tool batch immediately after cancellation", async () => {
    const abort = new AbortController(); let executions = 0;
    const client = new OpenAIClient("fixture", "fixture", undefined, async () => response([call("read_file", {path: "app.ts", start: 1}), call("list_files", {contains: "", offset: 0}, "call-2")]));
    await expect(client.run(options({signal: abort.signal, onTool() {executions++; abort.abort(new Error("cancelled fixture")); return "source";}}))).rejects.toThrow("cancelled fixture");
    expect(executions).toBe(1);
  });
  test("rejects duplicate and missing call identifiers", async () => {
    for (const output of [[call("list_files", {contains: "", offset: 0}, "")], [call("list_files", {contains: "", offset: 0}), call("list_files", {contains: "", offset: 0})]]) {
      const client = new OpenAIClient("fixture", "fixture", undefined, async () => response(output));
      await expect(client.run(options())).rejects.toThrow("invalid tool call");
    }
  });
  test("provider-controlled status, codes and transport errors never enter diagnostics", async () => {
    const privateText = "private_secret_material";
    const transports = [
      async () => response(text, privateText),
      async () => {throw new Error(privateText);},
      async () => new Response(privateText, {status: 200}),
      async () => Response.json({error: {code: privateText, message: privateText, param: privateText}}, {status: 400}),
    ];
    for (const transport of transports) {
      const error = await new OpenAIClient("fixture", "fixture", undefined, transport).run(options()).catch(error => error);
      expect(error).toBeInstanceOf(Error); expect(error.message).not.toContain(privateText);
    }
  });
  test("tool budget rejects further actions and reports the stop reason", async () => {
    let executions = 0, finish = "";
    const client = new OpenAIClient("fixture", "fixture", undefined, async () => response([call("list_files", {contains: "", offset: 0})]));
    await expect(client.run(options({steps: 0, onFinish(reason) {finish = reason;}, onTool() {executions++;}}))).rejects.toThrow("tool budget");
    expect(executions).toBe(0); expect(finish).toBe("tool_limit");
  });
});
