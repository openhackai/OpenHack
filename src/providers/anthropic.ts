import { z } from "zod";
import type { AgentClient, AgentOptions, ReasoningEffort } from "../harness/openai";
import { systemPrompt } from "../harness/prompts";
import { scanTools, parseScanToolArguments } from "../harness/tools";

type Request = (url: string, init: RequestInit) => Promise<Response>;
type Block = { type: string; [key: string]: unknown };
type Message = { role: "user" | "assistant"; content: string | Block[] };
const responseSchema = z.object({
  content: z.array(z.object({ type: z.string() }).passthrough()),
  stop_reason: z.string().nullable(),
  stop_details: z.object({ type: z.string() }).passthrough().nullish(),
  usage: z.object({
    input_tokens: z.number().nonnegative(), output_tokens: z.number().nonnegative(),
    cache_read_input_tokens: z.number().nonnegative().nullish(),
    cache_creation_input_tokens: z.number().nonnegative().nullish(),
  }),
});
const toolCallSchema = z.object({ type: z.literal("tool_use"), id: z.string().min(1), name: z.string(), input: z.record(z.string(), z.unknown()) });
const textSchema = z.object({ type: z.literal("text"), text: z.string() });
const unsupportedConstraints = new Set(["minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "minLength", "maxLength", "pattern", "maxItems", "uniqueItems", "minProperties", "maxProperties"]);
const supportedFormats = new Set(["date-time", "time", "date", "duration", "email", "hostname", "uri", "ipv4", "ipv6", "uuid"]);

/** Claude's grammar accepts a subset; original Zod validation still enforces every bound. */
export function anthropicOutputSchema(schema: Record<string, unknown>): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  const constraints: string[] = [];
  for (const [key, value] of Object.entries(schema)) {
    if (key === "$schema") continue;
    if (unsupportedConstraints.has(key) || (key === "minItems" && value !== 0 && value !== 1) || (key === "format" && !supportedFormats.has(String(value)))) {
      constraints.push(`${key}: ${JSON.stringify(value)}`); continue;
    }
    if (["properties", "$defs", "definitions"].includes(key) && value && typeof value === "object" && !Array.isArray(value)) {
      result[key] = Object.fromEntries(Object.entries(value).map(([name, child]) => [name, anthropicOutputSchema(child as Record<string, unknown>)]));
    } else if (["anyOf", "allOf", "oneOf"].includes(key) && Array.isArray(value)) {
      result[key === "oneOf" ? "anyOf" : key] = value.map(child => anthropicOutputSchema(child as Record<string, unknown>));
    } else if (key === "items" && value && typeof value === "object" && !Array.isArray(value)) {
      result[key] = anthropicOutputSchema(value as Record<string, unknown>);
    } else result[key] = value;
  }
  if (result.type === "object" || result.properties) result.additionalProperties = false;
  if (constraints.length) result.description = [schema.description, `Additional constraints: ${constraints.join("; ")}.`].filter(Boolean).join(" ");
  return result;
}

async function retryDelay(seconds: number, signal: AbortSignal): Promise<void> {
  signal.throwIfAborted();
  await new Promise<void>((resolve, reject) => {
    const abort = () => { clearTimeout(timer); reject(signal.reason); };
    const timer = setTimeout(() => { signal.removeEventListener("abort", abort); resolve(); }, seconds * 1000);
    signal.addEventListener("abort", abort, { once: true });
  });
}

export class AnthropicClient implements AgentClient {
  constructor(private key: string, private model: string, private request: Request = fetch, private reasoning?: ReasoningEffort) {
    if (!key.trim()) throw new Error("Set ANTHROPIC_API_KEY in your shell to scan with Anthropic.");
    if (reasoning === "none" || reasoning === "minimal") throw new Error("Anthropic reasoning must be low, medium, high, or xhigh. Use the model default to leave it unset.");
  }

  private async post(body: unknown, options: AgentOptions<unknown>): Promise<z.infer<typeof responseSchema>> {
    for (let attempt = 0; attempt < 3; attempt++) {
      await options.beforeRequest?.();
      options.signal.throwIfAborted();
      let response: Response;
      try {
        response = await this.request("https://api.anthropic.com/v1/messages", {
          method: "POST", headers: { "x-api-key": this.key, "anthropic-version": "2023-06-01", "Content-Type": "application/json" },
          body: JSON.stringify(body), redirect: "error", signal: AbortSignal.any([options.signal, AbortSignal.timeout(300_000)]),
        });
        if (response.ok) {
          const value: unknown = await response.json();
          const parsed = responseSchema.safeParse(value);
          if (!parsed.success) throw new Error("Anthropic returned an invalid response; this pass is incomplete.");
          return parsed.data;
        }
      } catch (error) {
        if (options.signal.aborted) throw options.signal.reason;
        const detail = (error && typeof error === "object" ? error : {}) as { name?: string; code?: string };
        const transient = error instanceof TypeError || ["TimeoutError", "AbortError"].includes(detail.name ?? "") || ["ECONNRESET", "ETIMEDOUT", "ECONNREFUSED", "EPIPE"].includes(detail.code ?? "");
        if (!transient) throw new Error("Anthropic returned an invalid response; this pass is incomplete.");
        if (attempt === 2) throw new Error("Anthropic request failed after 3 attempts (timeout or connection failure). The task can be resumed.");
        options.onEvent(`Provider request interrupted; retry ${attempt + 2}/3 after ${2 ** attempt}s.`);
        await retryDelay(2 ** attempt, options.signal); continue;
      }
      if ((response.status === 429 || response.status >= 500) && attempt < 2) {
        const seconds = Math.min(10, Math.max(1, Number(response.headers.get("retry-after")) || 2 ** attempt));
        await response.body?.cancel();
        options.onEvent(`Provider HTTP ${response.status}; retry ${attempt + 2}/3 after ${seconds}s.`);
        await retryDelay(seconds, options.signal); continue;
      }
      await response.body?.cancel();
      const hint = response.status === 401 || response.status === 403 ? "Check ANTHROPIC_API_KEY and model access."
        : response.status === 400 ? "Request rejected; check model support and request parameters."
        : response.status === 404 ? "Check the model ID."
        : response.status === 429 ? "Rate limit or quota exceeded." : "The provider request failed.";
      // Never reflect provider bodies: they can contain source excerpts or credentials.
      throw new Error(`Anthropic HTTP ${response.status}. ${hint}`);
    }
    throw new Error("Anthropic retry limit reached.");
  }

  async run<T>(options: AgentOptions<T>): Promise<T> {
    const messages: Message[] = [{ role: "user", content: `${options.prompt}\n\nCoordinator budget: at most ${options.steps} tool rounds. Focus on your assigned role and source objectives. Return the requested JSON as soon as the review is finished. If evidence is missing, explicitly mark it unreviewed or uncertain rather than claiming completion.` }];
    const schema = anthropicOutputSchema(z.toJSONSchema(options.schema));
    for (let step = 0; step <= options.steps; step++) {
      options.signal.throwIfAborted();
      if (step > 0 && (step === Math.floor(options.steps * 0.75) || step === options.steps - 1)) {
        messages.push({ role: "user", content: `Coordinator checkpoint: ${options.steps - step} tool round(s) remain. Complete decisive evidence checks, then return the requested JSON. Identify unresolved source objectives explicitly; do not expand scope.` });
      }
      options.onEvent(`${options.name} · ${step === options.steps ? "completing" : `step ${step + 1}`}`);
      const response = await this.post({
        model: this.model, system: systemPrompt, messages, max_tokens: 10000,
        tools: scanTools.map(tool => ({ name: tool.name, description: tool.description, input_schema: tool.parameters, strict: true })),
        tool_choice: step === options.steps ? { type: "none" } : { type: "auto", disable_parallel_tool_use: true },
        output_config: { format: { type: "json_schema", schema }, ...(this.reasoning ? { effort: this.reasoning } : {}) },
      }, options);
      options.signal.throwIfAborted();
      const usage = response.usage;
      options.onUsage(usage.input_tokens + (usage.cache_read_input_tokens ?? 0) + (usage.cache_creation_input_tokens ?? 0), usage.output_tokens, usage.cache_read_input_tokens ?? 0);
      if (response.stop_reason === "refusal" || response.stop_details?.type === "refusal") throw new Error("The model declined this pass; no complete scan was produced.");
      const calls = response.content.filter(block => block.type === "tool_use");
      if (response.stop_reason === "tool_use") {
        if (step === options.steps) { options.onFinish?.("tool_limit"); throw new Error("Agent exceeded its tool budget."); }
        if (!calls.length || calls.length > 8) throw new Error("Anthropic returned an invalid number of tool calls; this pass is incomplete.");
        // Preserve thinking/signature blocks unchanged alongside tool calls.
        messages.push({ role: "assistant", content: response.content });
        const results: Block[] = [];
        const ids = new Set<string>();
        for (const block of calls) {
          options.signal.throwIfAborted();
          const parsed = toolCallSchema.safeParse(block);
          if (!parsed.success || ids.has(parsed.data.id)) throw new Error("Anthropic returned an invalid tool call; this pass is incomplete.");
          const call = parsed.data;
          ids.add(call.id);
          let result: unknown, failed = false;
          try {
            const args = parseScanToolArguments(call.name, call.input);
            result = await options.onTool(call.name, args);
          } catch { result = { error: "Invalid tool arguments or unavailable source" }; failed = true; }
          options.signal.throwIfAborted();
          results.push({ type: "tool_result", tool_use_id: call.id, content: JSON.stringify(result ?? null), ...(failed ? { is_error: true } : {}) });
        }
        messages.push({ role: "user", content: results });
        continue;
      }
      if (response.stop_reason !== "end_turn" || calls.length) throw new Error("Anthropic stopped before completing this pass; scan results are incomplete.");
      const text = response.content.filter(block => block.type === "text").map(block => textSchema.safeParse(block)).map(parsed => parsed.success ? parsed.data.text : "").join("");
      let result: T;
      try { result = options.schema.parse(JSON.parse(text)); }
      catch { throw new Error(`${options.name} returned invalid structured output; scan results are incomplete.`); }
      options.onFinish?.(step === options.steps ? "tool_limit" : "completed");
      return result;
    }
    throw new Error("Agent did not finish.");
  }
}
