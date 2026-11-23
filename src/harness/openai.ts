import { z } from "zod";
import { systemPrompt } from "./prompts";

import { scanTools, parseScanToolArguments } from "./tools";
const tools = scanTools.map(tool => ({type: "function", strict: true, ...tool}));
export interface AgentOptions<T> {
  name: string; prompt: string; schema: z.ZodType<T>; signal: AbortSignal; steps: number;
  onTool: (name: string, args: Record<string, unknown>) => unknown;
  onUsage: (input: number, output: number, cached?: number) => void;
  onEvent: (message: string) => void;
  beforeRequest?: () => Promise<void>;
  onFinish?: (reason: "completed" | "tool_limit") => void;
}
export interface AgentClient { run<T>(options: AgentOptions<T>): Promise<T> }
export const reasoningEfforts = ["none", "minimal", "low", "medium", "high", "xhigh"] as const;
export type ReasoningEffort = typeof reasoningEfforts[number];
type Output = { type: string; name?: string; arguments?: string; call_id?: string; content?: { type: string; text?: string }[] };
const responseSchema = z.object({
  status: z.string(), output: z.array(z.object({ type: z.string() }).passthrough()),
  usage: z.object({ input_tokens: z.number().nonnegative(), output_tokens: z.number().nonnegative(), input_tokens_details: z.object({ cached_tokens: z.number().nonnegative() }).optional() }).nullish(),
});

export class OpenAIClient implements AgentClient {
  constructor(private key: string, private model: string, private base = "https://api.openai.com/v1", private request: (url: string, init: RequestInit) => Promise<Response> = fetch, private reasoningEffort?: ReasoningEffort) {
    if (!key.trim()) throw new Error("Set OPENAI_API_KEY in your shell to scan.");
  }
  private async post(body: unknown, signal: AbortSignal, beforeRequest?: () => Promise<void>, onEvent?: (message: string) => void) {
    const wait = async (seconds: number) => {
      signal.throwIfAborted();
      await new Promise<void>((resolve, reject) => {
        const abort = () => { clearTimeout(timer); reject(signal.reason); };
        const timer = setTimeout(() => { signal.removeEventListener("abort", abort); resolve(); }, seconds * 1000);
        signal.addEventListener("abort", abort, { once: true });
      });
    };
    for (let attempt = 0; attempt < 3; attempt++) {
      await beforeRequest?.();
      signal.throwIfAborted();
      let response: Response;
      try {
        response = await this.request(`${this.base}/responses`, {
          method: "POST", headers: { Authorization: `Bearer ${this.key}`, "Content-Type": "application/json" },
          body: JSON.stringify(body), redirect: "error", signal: AbortSignal.any([signal, AbortSignal.timeout(300_000)]),
        });
        if (response.ok) return responseSchema.parse(await response.json());
      } catch (error: any) {
        if (signal.aborted) throw signal.reason;
        const transient = error instanceof TypeError || ["TimeoutError", "AbortError"].includes(error?.name) || ["ECONNRESET", "ETIMEDOUT", "ECONNREFUSED", "EPIPE"].includes(error?.code);
        if (!transient) throw new Error("OpenAI returned an invalid response; this pass is incomplete.");
        if (attempt === 2) throw new Error("OpenAI request failed after 3 attempts (timeout or connection failure). The task can be resumed.");
        onEvent?.(`Provider request interrupted; retry ${attempt + 2}/3 after ${2 ** attempt}s.`);
        await wait(2 ** attempt); continue;
      }
      if ((response.status === 429 || response.status >= 500) && attempt < 2) {
        const seconds = Math.min(10, Math.max(1, Number(response.headers.get("retry-after")) || 2 ** attempt));
        await response.body?.cancel();
        onEvent?.(`Provider HTTP ${response.status}; retry ${attempt + 2}/3 after ${seconds}s.`);
        await wait(seconds);
        continue;
      }
      // Do not echo response bodies: provider errors may contain source or credentials.
      let detail = "";
      if (response.status === 400) {
        const value: unknown = await response.json().catch(() => null);
        const body = value && typeof value === "object" ? value as {error?: {param?: unknown}} : {};
        const param = body.error?.param;
        // Only known API field names; never reflect the provider message or arbitrary values.
        if (typeof param === "string" && /^(model|reasoning(?:\.effort)?|text\.format(?:\.(?:name|schema))?|input|max_output_tokens|tools)$/.test(param)) detail = ` (${param})`;

      }
      await response.body?.cancel().catch(() => {});
      const hint = response.status === 401 ? "Check OPENAI_API_KEY." : response.status === 400 ? "Request rejected; check model support and request parameters." : response.status === 404 ? "Check the model ID and endpoint." : response.status === 429 ? "Rate limit or quota exceeded." : "The provider request failed.";
      throw new Error(`OpenAI HTTP ${response.status}${detail}. ${hint}`);
    }
    throw new Error("OpenAI retry limit reached.");
  }
  async run<T>(options: AgentOptions<T>): Promise<T> {
    const input: unknown[] = [{ role: "user", content: `${options.prompt}\n\nCoordinator budget: at most ${options.steps} tool rounds. Focus on your assigned role and source objectives. Return the requested JSON as soon as the review is finished; do not keep exploring unrelated code. If evidence is missing, explicitly mark it unreviewed or uncertain rather than claiming completion.` }];
    for (let step = 0; step <= options.steps; step++) {
      options.signal.throwIfAborted();
      if (step > 0 && (step === Math.floor(options.steps * 0.75) || step === options.steps - 1)) {
        input.push({ role: "user", content: `Coordinator checkpoint: ${options.steps - step} tool round(s) remain. Complete only decisive evidence checks, then return the requested JSON. Identify unresolved source objectives explicitly; do not expand scope.` });
      }
      options.onEvent(`${options.name} · ${step === options.steps ? "completing" : `step ${step + 1}`}`);
      const response = await this.post({
        model: this.model, instructions: systemPrompt, input, store: false,
        ...(this.reasoningEffort ? { reasoning: { effort: this.reasoningEffort } } : {}),
        include: ["reasoning.encrypted_content"], max_output_tokens: 10000,
        tools, tool_choice: step === options.steps ? "none" : "auto", parallel_tool_calls: false,
        text: { format: { type: "json_schema", name: options.name.replace(/[^a-zA-Z0-9]/g, "_").slice(0, 64) || "Agent", strict: true, schema: z.toJSONSchema(options.schema) } },
      }, options.signal, options.beforeRequest, options.onEvent);
      options.onUsage(response.usage?.input_tokens ?? 0, response.usage?.output_tokens ?? 0, response.usage?.input_tokens_details?.cached_tokens ?? 0);
      options.signal.throwIfAborted();
      if (response.status !== "completed") throw new Error("OpenAI stopped before completing this pass; scan results are incomplete.");
      const output = response.output as Output[];
      if (!Array.isArray(output)) throw new Error("OpenAI response has no output.");
      if (output.some(item => !["message", "reasoning", "function_call"].includes(item.type))) throw new Error("OpenAI returned an unsupported output type; this pass is incomplete.");
      input.push(...output);
      const calls = output.filter(o => o.type === "function_call");
      if (calls.length) {
        if (step === options.steps) { options.onFinish?.("tool_limit"); throw new Error("Agent exceeded its tool budget."); }
        if (calls.length > 8) throw new Error("Agent requested too many tools in one step.");
        const ids = new Set<string>();
        for (const call of calls) {
          options.signal.throwIfAborted();
          if (typeof call.call_id !== "string" || !call.call_id || ids.has(call.call_id)) throw new Error("OpenAI returned an invalid tool call; this pass is incomplete.");
          ids.add(call.call_id);
          let result: unknown;
          try {
            const args = parseScanToolArguments(call.name ?? "", JSON.parse(call.arguments ?? "{}"));
            result = await options.onTool(call.name ?? "", args);
          } catch { result = { error: "Invalid tool arguments or unavailable source" }; }
          options.signal.throwIfAborted();
          input.push({ type: "function_call_output", call_id: call.call_id, output: JSON.stringify(result ?? null) });
        }
        continue;
      }
      const content = output.flatMap(o => o.content ?? []);
      if (content.some(c => c.type === "refusal")) throw new Error("The model declined this pass; no complete scan was produced.");
      const text = content.filter(c => c.type === "output_text").map(c => c.text ?? "").join("");
      try { const result = options.schema.parse(JSON.parse(text)); options.onFinish?.(step === options.steps ? "tool_limit" : "completed"); return result; }
      catch { throw new Error(`${options.name} returned invalid structured output; scan results are incomplete.`); }
    }
    throw new Error("Agent did not finish.");
  }
}
