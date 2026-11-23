import { parseArgs } from "node:util";
import { startScanSchema, type StartScanInput } from "../shared/contracts";
import { safe } from "./display";

const providerFlags = ["openai", "anthropic", "codex", "claude-code"] as const;
export function parseCli(args: string[]) {
  return parseArgs({ args, allowPositionals: true, strict: true, options: {
    help: { type: "boolean", short: "h" }, version: { type: "boolean", short: "v" },
    json: { type: "boolean" }, "no-open": { type: "boolean" }, all: { type: "boolean" },
    openai: { type: "boolean" }, anthropic: { type: "boolean" },
    codex: { type: "boolean" }, "claude-code": { type: "boolean" },
    provider: { type: "string" }, port: { type: "string" }, home: { type: "string" }, model: { type: "string" },
    workers: { type: "string" }, steps: { type: "string" }, reasoning: { type: "string" },
    context: { type: "string" }, "fail-on": { type: "string" }, state: { type: "string" },
    severity: { type: "string" },
  } });
}
export type CliValues = ReturnType<typeof parseCli>["values"];
export function scanOptions(values: CliValues): Omit<StartScanInput, "path"> {
  const selected = providerFlags.filter(flag => values[flag]);
  if (selected.length + (values.provider !== undefined ? 1 : 0) > 1) {
    throw new Error("Choose one provider: --codex, --claude-code, --openai, --anthropic, or --provider ID.");
  }
  const options: Record<string, unknown> = {};
  for (const key of ["provider", "model", "workers", "steps", "reasoning", "context"] as const) {
    if (values[key] !== undefined) options[key] = key === "workers" || key === "steps" ? Number(values[key]) : values[key];
  }
  if (selected[0]) options.provider = selected[0];
  return startScanSchema.omit({ path: true }).parse(options);
}

const aliases: Record<string, string> = { history: "scans", list: "findings", settings: "config", doctor: "providers" };
export const commandName = (name?: string): string | undefined => name === undefined ? undefined : aliases[name] ?? name;
const commands: Record<string, { usage: string; description: string; flags: string[]; min: number; max: number }> = {
  scan: { usage: "scan [directory]", description: "Scan the current directory, or a supplied project path.\n\nProvider: --codex | --claude-code | --openai | --anthropic | --provider ID\nOptions:  --model ID --workers 1..32 --steps 1..30\n          --reasoning LEVEL --context TEXT --fail-on SEVERITY --json\n\nExamples:\n  openhack scan\n  openhack scan --codex\n  openhack scan --claude-code\n  openhack scan ../project --openai --model gpt-5-mini\n\nProvider shortcuts apply only to this scan; they do not change saved settings.\nExit codes: 0 complete, 1 failed/partial, 2 fail-on threshold, 130 cancelled.", flags: [...providerFlags, "provider", "model", "workers", "steps", "reasoning", "context", "fail-on", "json"], min: 0, max: 1 },
  dashboard: { usage: "dashboard [start|stop|status]", description: "Without an action, run in the foreground until Ctrl+C.\nstart: start/reuse the background dashboard and open it.\nstop: checkpoint active dashboard scans and stop the server.\nstatus: show whether the dashboard is running.\n\nOptions: --port PORT --no-open (start/foreground) --json (status/stop)", flags: ["port", "no-open", "json"], min: 0, max: 1 },
  scans: { usage: "scans [directory] [--all] [--json]", description: "List scans for the current directory. Supply a path for another project,\nor --all for all projects. Alias: history.", flags: ["all", "json"], min: 0, max: 1 },
  findings: { usage: "findings [directory] [--all] [--severity LEVEL] [--state STATE] [--json]", description: "List current findings for the current directory, a supplied path, or --all projects.\nSeverity: critical, high, medium, low. State: open, resolved, dismissed.\nAlias: list.", flags: ["all", "severity", "state", "json"], min: 0, max: 1 },
  show: { usage: "show <scan-id> [--json]", description: "Show a saved scan from any project.", flags: ["json"], min: 1, max: 1 },
  resume: { usage: "resume <scan-id> [--steps 1..30] [--fail-on LEVEL] [--json]", description: "Resume unfinished work using its saved project, provider, model and settings.\nThe source snapshot must be unchanged. Ctrl+C saves a checkpoint.", flags: ["steps", "fail-on", "json"], min: 1, max: 1 },
  status: { usage: "status <scan-id:finding-id> <open|resolved|dismissed> [--json]", description: "Update a finding's triage status. Use dashboard status for server status.", flags: ["json"], min: 2, max: 2 },
  note: { usage: "note <scan-id:finding-id> <text> [--json]", description: "Save a finding note. Quote text containing spaces; use an empty string to clear it.", flags: ["json"], min: 2, max: 2 },
  providers: { usage: "providers [--json]", description: "Check API-key configuration and existing Codex/Claude Code subscription logins.\nSubscription providers use the installed CLI login without extra configuration.\nAlias: doctor.", flags: ["json"], min: 0, max: 0 },
  config: { usage: "config [set <key> <value>] [--json]", description: "Show settings, or set provider, model, workers, steps, reasoning, context or port.\nUse reasoning default to clear an override. Configure API keys in the dashboard\nor with OPENAI_API_KEY / ANTHROPIC_API_KEY. Alias: settings.", flags: ["json"], min: 0, max: 3 },
  version: { usage: "version", description: "Print the installed OpenHack version.", flags: [], min: 0, max: 0 },
};

/** Reject ignored options/extra arguments before touching storage or starting work. */
export function validateCli(positionals: string[], values: CliValues): void {
  const name = commandName(positionals[0]);
  const definition = name === undefined ? { flags: ["port", "no-open"], min: 0, max: 0, usage: "[--port PORT] [--no-open]" } : commands[name];
  if (!definition) throw new Error(`Unknown command: ${safe(name)}. Use openhack help.`);
  const allowed = new Set(["home", "help", "version", ...definition.flags]);
  for (const key of Object.keys(values)) if (!allowed.has(key)) throw new Error(`--${safe(key)} is not supported here. Use openhack help ${name ?? ""}.`);
  const count = Math.max(0, positionals.length - 1);
  if (count < definition.min || count > definition.max) throw new Error(`Usage: openhack ${definition.usage}`);
  if ((name === "scans" || name === "findings") && values.all && count) throw new Error("Choose a project directory or --all, not both.");
  if (name === "dashboard") {
    const action = positionals[1];
    if (action !== undefined && !["start", "stop", "status"].includes(action)) throw new Error("Use openhack dashboard start, stop, or status.");
    if (values["no-open"] && (action === "stop" || action === "status")) throw new Error("--no-open applies to starting the dashboard.");
    if (values.json && action !== "stop" && action !== "status") throw new Error("--json applies to dashboard status or stop.");
  }
  if (name === "config" && count && (positionals[1] !== "set" || count !== 3)) throw new Error("Use openhack config or openhack config set <key> <value>.");
}

export function helpFor(topic?: string): string {
  if (!topic || topic === "help") return help;
  const definition = commands[commandName(topic)!];
  if (!definition) throw new Error(`Unknown help topic: ${safe(topic)}. Use openhack help.`);
  return `Usage: openhack ${definition.usage}\n\n${definition.description}\n\nGlobal: --home DIRECTORY --help --version\nData: ~/.openhack (or OPENHACK_HOME)\n`;
}
export const help = `OpenHack — local source scanning and findings

Usage: openhack [command] [options]

Run openhack to open the local dashboard, starting it in the background if needed.

  scan [directory]             Scan the current directory or a supplied project
  scans [directory]            Scan history for this directory (--all for all)
  findings [directory]         Findings for this directory (--all for all)
  show <scan-id>               Show a saved scan
  resume <scan-id>             Resume unfinished work
  status <scan-id:finding-id> <open|resolved|dismissed>
  note <scan-id:finding-id> <text>
  dashboard [start|stop|status] Manage the dashboard; no action runs foreground
  providers                   Check provider availability (alias: doctor)
  config [set <key> <value>]   Show or update settings (alias: settings)
  help [command]              Show command-specific help
  version                     Print installed version

Examples:
  openhack scan
  openhack scan --codex
  openhack scan --claude-code
  openhack scan --openai --model gpt-5-mini
  openhack scan --anthropic
  openhack dashboard stop
  openhack help scan

Scan provider shortcuts override the saved provider for that scan only.
Use --json on reports and lists for automation. Global: --home DIRECTORY -h -v
Data: ~/.openhack (or OPENHACK_HOME)
Use saved API keys, OPENAI_API_KEY / ANTHROPIC_API_KEY, or existing CLI logins.
Source excerpts are sent to the selected provider. No OpenHack account required.
`;
