import { z } from "zod";
import { parseScanToolArguments, scanToolArgumentSchemas, scanToolCallSchema, type ScanToolCall, type ScanToolName } from "../harness/tools";

export interface Envelope<T> { toolCalls: ScanToolCall[]; result: T | null }
/** Each CLI must constrain the real task result, not an opaque JSON-encoded string. */
export function createEnvelopeSchema<T>(taskSchema: z.ZodType<T>) {
  const response = z.discriminatedUnion("kind", [
    z.object({kind: z.literal("tools"), toolCalls: z.array(scanToolCallSchema).min(1).max(8)}).strict(),
    z.object({kind: z.literal("result"), result: taskSchema}).strict(),
  ]);
  return z.object({response}).strict();
}
export function parseToolArguments(call: ScanToolCall): Record<string, unknown> {
  return parseScanToolArguments(call.name, call.args);
}
type JsonRecord = Record<string, unknown>;
function record(value: unknown): JsonRecord | undefined { return value !== null && typeof value === "object" && !Array.isArray(value) ? value as JsonRecord : undefined; }
function parsed(line: string): JsonRecord | undefined {
  if (!line.trim()) return undefined;
  try { const item = record(JSON.parse(line)); if (!item) throw new Error(); return item; }
  catch { throw new Error("Subscription CLI returned an invalid JSON event."); }
}
/** Fail closed on reported native actions; StructuredOutput only serializes the schema result. */
export function inspectEvent(provider: "codex" | "claude", line: string): void {
  const event = parsed(line); if (!event) return;
  if (provider === "codex") {
    if (!["thread.started", "turn.started", "item.started", "item.updated", "item.completed", "turn.completed", "turn.failed", "error"].includes(String(event.type))) throw new Error("Codex emitted an unsupported event; the scan was stopped.");
    if (typeof event.type === "string" && event.type.startsWith("item.")) {
      const item = record(event.item);
      // Error items can be nonfatal startup notices (for example disabled Code Mode).
      // They execute no tool; turn.failed and missing final results still fail closed.
      if (!item || !["agent_message", "reasoning", "error"].includes(String(item.type))) throw new Error("Codex attempted a native tool action. The scan was stopped.");
    }
    if (["turn.failed", "error"].includes(String(event.type))) throw new Error("Codex could not complete this request. Check subscription access and model availability.");
  } else {
    if (!["system", "assistant", "user", "result", "rate_limit_event"].includes(String(event.type))) throw new Error("Claude Code emitted an unsupported event; the scan was stopped.");
    if (event.type === "system" && typeof event.subtype === "string" && event.subtype.includes("hook")) throw new Error("Claude Code ran a hook despite scanner restrictions.");
    if (event.type === "system" && event.subtype === "init") {
      if (Array.isArray(event.tools) && event.tools.some(tool => tool !== "StructuredOutput")) throw new Error("Claude Code exposed native tools despite scanner restrictions.");
      if (Array.isArray(event.mcp_servers) && event.mcp_servers.length) throw new Error("Claude Code connected external tools despite scanner restrictions.");
    }
    const message = record(event.message);
    if (Array.isArray(message?.content)) for (const value of message.content) {
      const content = record(value);
      if (content?.type === "tool_use" && content.name !== "StructuredOutput") throw new Error("Claude Code attempted a native tool action. The scan was stopped.");
    }
    if (event.type === "result" && (event.is_error === true || event.subtype !== "success")) throw new Error("Claude Code could not complete this request. Check subscription access and model availability.");
  }
}
/** Raised only after an isolated Claude invocation requests our exact typed source operations. */
class ClaudeProtocolInterruption extends Error {
  private valid = true;
  get recoverable(): boolean { return this.valid; }
  invalidate(): void { this.valid = false; }
}
export class ClaudeSourceRequest extends ClaudeProtocolInterruption {
  constructor(readonly calls: ScanToolCall[]) {
    super("Claude requested scanner operations through the wrong transport.");
    this.name = "ClaudeSourceRequest";
  }
}

/** Contains only schema-owned diagnostics; the invalid arguments are not retained. */
export class ClaudeSourceCorrection extends ClaudeProtocolInterruption {
  constructor(readonly diagnostics: string) {
    super(`Claude Code requested invalid arguments for a known source operation. ${diagnostics}`);
    this.name = "ClaudeSourceCorrection";
  }
}

function sourceIssues(name: ScanToolName, index: number, issues: readonly z.core.$ZodIssue[]): string {
  const fields = new Set(Object.keys(scanToolArgumentSchemas[name].shape));
  return issues.slice(0, 5).map(issue => {
    const path = issue.path.slice(0, 4).map(part => typeof part === "string" && fields.has(part) ? part : "<field>").join(".");
    return `toolCalls[${index}].${name}.args${path ? `.${path}` : ""}: ${issue.code}`;
  }).join("; ");
}

/**
 * The CLI advertises only StructuredOutput, so these scanner names cannot execute
 * there. Stop the process first; the adapter may then replay them through its own
 * read-only snapshot callbacks. The default event validator remains strict.
 */
export function createClaudeProtocolMonitor(): (line: string) => void {
  let isolated = false;
  let pending: ClaudeSourceRequest | ClaudeSourceCorrection | undefined;
  const observe = (line: string): void => {
    const event = parsed(line);
    if (!event) return;
    if (pending) {
      inspectEvent("claude", line);
      const message = record(event.message);
      if (event.type === "result" || (Array.isArray(message?.content) && message.content.some(value => record(value)?.type === "tool_use"))) throw new Error("Claude Code emitted conflicting tool events after a source request.");
      return;
    }
    if (event.type === "system" && event.subtype === "init") {
      inspectEvent("claude", line);
      if (!Array.isArray(event.tools) || event.tools.length !== 1 || event.tools[0] !== "StructuredOutput" || !Array.isArray(event.mcp_servers) || event.mcp_servers.length !== 0) throw new Error("Claude Code did not confirm the required isolated tool configuration.");
      isolated = true;
      return;
    }
    if ((event.type === "assistant" || event.type === "result") && !isolated) throw new Error("Claude Code did not confirm the required isolated tool configuration.");
    if (event.type === "assistant") {
      const message = record(event.message);
      const calls = Array.isArray(message?.content) ? message.content.map(record).filter(item => item?.type === "tool_use") : [];
      if (calls.some(call => call?.name !== "StructuredOutput")) {
        // Validate the entire message before offering any recovery. Never split a
        // mixed native/source batch or accept source arguments by coercion.
        if (calls.some(call => call?.name === "StructuredOutput") || calls.length > 8) throw new Error("Claude Code attempted a mixed or oversized native tool batch. The scan was stopped.");
        const ids = new Set<string>();
        for (const call of calls) {
          if (typeof call?.id !== "string" || !call.id || ids.has(call.id)) throw new Error("Claude Code emitted an invalid source request identifier.");
          ids.add(call.id);
        }
        if (calls.some(call => typeof call?.name !== "string" || !Object.hasOwn(scanToolArgumentSchemas, call.name))) throw new Error("Claude Code attempted an unknown native tool. The scan was stopped.");
        const diagnostics: string[] = [];
        for (const [index, call] of calls.entries()) {
          const name = call!.name as ScanToolName;
          const checked = scanToolArgumentSchemas[name].safeParse(call!.input);
          if (!checked.success) diagnostics.push(sourceIssues(name, index, checked.error.issues));
        }
        if (diagnostics.length) pending = new ClaudeSourceCorrection(diagnostics.join("; "));
        else pending = new ClaudeSourceRequest(z.array(scanToolCallSchema).min(1).max(8).parse(calls.map(call => ({name: call!.name, args: call!.input}))));
        throw pending;
      }
    }
    inspectEvent("claude", line);
  };
  return line => {
    try { observe(line); }
    catch (error) {
      // runCli retains the first error while draining buffered output. A later
      // violation must invalidate that same object instead of being swallowed.
      if (pending && error !== pending) pending.invalidate();
      throw error;
    }
  };
}

export interface CliAnswer<T> { envelope: Envelope<T>; input: number; output: number; cached: number }
export function parseAnswer<T>(provider: "codex" | "claude", output: string, taskSchema: z.ZodType<T>, onUsage?: (input: number, output: number, cached: number) => void): CliAnswer<T> {
  const events = output.split(/\r?\n/).filter(line => line.trim()).map(line => { inspectEvent(provider, line); return parsed(line)!; });
  let text: string | undefined, structured: unknown, input = 0, tokens = 0, cached = 0, completed = false;
  for (const event of events) {
    if (provider === "codex") {
      const item = record(event.item);
      if (event.type === "item.completed" && item?.type === "agent_message" && typeof item.text === "string") text = item.text;
      if (event.type === "turn.completed") {
        completed = true; const usage = record(event.usage);
        input += numeric(usage?.input_tokens); tokens += numeric(usage?.output_tokens); cached += numeric(usage?.cached_input_tokens);
      }
    } else if (event.type === "result") {
      completed = true; structured = event.structured_output;
      if (typeof event.result === "string") text = event.result;
      const usage = record(event.usage);
      cached += numeric(usage?.cache_read_input_tokens);
      input += numeric(usage?.input_tokens) + numeric(usage?.cache_creation_input_tokens) + numeric(usage?.cache_read_input_tokens);
      tokens += numeric(usage?.output_tokens);
      if (Array.isArray(event.permission_denials) && event.permission_denials.length) throw new Error("Claude Code requested unavailable native tools. The scan is incomplete.");
    }
  }
  if (!completed) throw new Error("Subscription CLI ended without completing its response.");
  onUsage?.(input, tokens, cached);
  const invalid = "Subscription CLI returned invalid scanner output; this pass is incomplete.";
  let value: unknown;
  try { value = structured ?? JSON.parse(text ?? ""); }
  catch { throw new Error(invalid); }
  const checked = createEnvelopeSchema(taskSchema).safeParse(value);
  if (!checked.success) throw new Error(`${invalid} ${validationIssues(checked.error.issues, taskSchema)}`);
  const response = checked.data.response;
  const envelope: Envelope<T> = response.kind === "tools"
    ? {toolCalls: response.toolCalls, result: null}
    : {toolCalls: [], result: response.result};
  return {envelope, input, output: tokens, cached};
}

/** Only schema-owned field names, indices, and Zod codes; never expected/received values or messages. */
function validationIssues<T>(issues: readonly z.core.$ZodIssue[], taskSchema: z.ZodType<T>): string {
  const fields = new Set<string>();
  const collect = (value: unknown): void => {
    if (!value || typeof value !== "object") return;
    if (Array.isArray(value)) {for (const child of value) collect(child); return;}
    const schema = value as Record<string, unknown>;
    if (schema.properties && typeof schema.properties === "object") for (const key of Object.keys(schema.properties)) fields.add(key);
    for (const child of Object.values(schema)) collect(child);
  };
  collect(z.toJSONSchema(createEnvelopeSchema(taskSchema)));
  return issues.slice(0, 5).map(issue => {
    const path = issue.path.slice(0, 8).map(part => typeof part === "number" ? `[${part}]` : typeof part === "string" && fields.has(part) ? part : "<field>").join(".") || "<root>";
    return `${path}: ${issue.code}`;
  }).join("; ");
}
function numeric(value: unknown): number { return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : 0; }
