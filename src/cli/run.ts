import { z } from "zod";
import { providerStatuses } from "../providers";
import { providerDefinitions } from "../shared/providers";
import { LocalWorkspace } from "../core/workspace";
import { runDashboard, startDashboardBackground, getDashboardStatus, stopDashboard } from "./dashboard";
import { readCredentials } from "../core/credentials";
import { rank, severities, statuses } from "../harness/types";
import { settingsPatchSchema } from "../shared/contracts";
import { parseCli, scanOptions, helpFor, commandName, validateCli } from "./args";
import { printScans, printFindings, printScan, safe } from "./display";

import { startScanProgress } from "./scan-progress";
import { version } from "../../package.json";

export async function runCli(args: string[], injectedWorkspace?: LocalWorkspace) {
  const {values, positionals} = parseCli(args);
  if (positionals[0] === "help") {
    if (positionals.length > 2) throw new Error("Usage: openhack help [command]");
    return console.log(helpFor(positionals[1]));
  }
  if (values.help) return console.log(helpFor(positionals[0]));
  if (values.version) return console.log(version);
  validateCli(positionals, values);
  const command = commandName(positionals[0]);
  if (command === "version") return console.log(version);
  const workspace = injectedWorkspace ?? new LocalWorkspace({home: values.home});
  const json = (value: unknown) => console.log(JSON.stringify(value, null, 2));
  const requireArg = (index: number, description: string) => {
    const value = positionals[index]; if (value === undefined) throw new Error(`Missing ${description}. Use openhack --help.`); return value;
  };
  if (command === undefined) {
    const settings = await workspace.getConfiguration();
    const port = z.coerce.number().int().min(1).max(65535).parse(values.port ?? settings.port);
    const dashboard = await startDashboardBackground(workspace, port, {open: !values["no-open"]});
    console.log(`OpenHack dashboard: ${dashboard.url}`);
    return;
  }
  if (command === "dashboard") {
    const settings = await workspace.getConfiguration();
    const port = z.coerce.number().int().min(1).max(65535).parse(values.port ?? settings.port);
    const action = positionals[1];
    if (action === "start") {
      const dashboard = await startDashboardBackground(workspace, port, {open: !values["no-open"]});
      return console.log(`OpenHack dashboard: ${dashboard.url}`);
    }
    if (action === "status" || action === "stop") {
      const status = await (action === "stop" ? stopDashboard(workspace, port) : getDashboardStatus(workspace, port));
      return values.json ? json(status) : console.log(`Dashboard: ${status.state} · ${status.url}${status.detail ? `\n${safe(status.detail)}` : ""}`);
    }
    return runDashboard(workspace, port, values["no-open"]);
  }
  if (command === "scan" || command === "resume") {
    const threshold = values["fail-on"] ? z.enum(severities).parse(values["fail-on"]) : undefined;
    const options = scanOptions(values);
    const progress = values.json ? undefined : startScanProgress(id => workspace.getScan(id));
    let id: string | undefined, stopping = false;
    const cancel = () => { stopping = true; progress?.cancel(); if (id) void workspace.cancelScan(id).catch(() => {}); };
    process.once("SIGINT", cancel); process.once("SIGTERM", cancel);
    try {
      if (command === "resume" && [values.provider, values.model, values.workers, values.reasoning, values.context].some(value => value !== undefined)) throw new Error("Resumed scans keep their saved configuration. Only --steps can be changed.");
      const scan = command === "scan" ? await workspace.startScan({path: positionals[1] ?? process.cwd(), ...options}) : await workspace.resumeScan(requireArg(1,"scan ID"), {steps: options.steps});
      id = scan.id;
      if (stopping) await workspace.cancelScan(id);
      progress?.attach(scan);
      const result = await workspace.waitForScan(scan.id);
      progress?.stop();
      if (values.json) json(result); else printScan(result);
      process.exitCode = result.state === "cancelled" ? 130 : result.state === "complete" ? 0 : 1;
      if (result.state === "complete" && threshold) {
        const findings = await workspace.listFindings(result.root);
        if (findings.some(f => f.scanId === result.id && f.status === "open" && f.validation === "validated" && rank(f.severity) <= rank(threshold))) process.exitCode = 2;
      }
    } finally { progress?.stop(); process.removeListener("SIGINT",cancel); process.removeListener("SIGTERM",cancel); await workspace.shutdown(); }
    return;
  }
  if (command === "scans" || command === "history") { const scans = await workspace.listScans(values.all ? undefined : positionals[1] ?? process.cwd()); return values.json ? json(scans) : printScans(scans); }
  if (command === "show") { const scan = await workspace.getScan(requireArg(1,"scan ID")); return values.json ? json(scan) : printScan(scan); }
  if (command === "findings" || command === "list") {
    const severity = values.severity ? z.enum(severities).parse(values.severity) : undefined;
    const state = values.state ? z.enum(statuses).parse(values.state) : undefined;
    const findings = (await workspace.listFindings(values.all ? undefined : positionals[1] ?? process.cwd())).filter(f => (!severity || f.severity === severity) && (!state || f.status === state));
    return values.json ? json(findings) : printFindings(findings);
  }
  if (command === "status" || command === "note") {
    const key = requireArg(1,"finding key"), parts = key.split(":");
    if (parts.length !== 2 || !parts[0] || !parts[1]) throw new Error("Use the full scan-id:finding-id key from openhack findings.");
    const value = requireArg(2,command === "status" ? "status" : "note text");
    const finding = await workspace.updateFinding(parts[0],parts[1],command === "status" ? {status:z.enum(statuses).parse(value)} : {note:value});
    return values.json ? json(finding) : console.log("Finding updated.");
  }
  if (command === "providers") {
    const providers = await providerStatuses(true, await readCredentials(workspace.home));
    return values.json ? json(providers) : console.log(providers.map(provider => `${providerDefinitions[provider.id].label} (${provider.id}): ${provider.ready ? "ready" : "setup required"}\n  ${safe(provider.detail)}`).join("\n\n"));
  }
  if (command === "config") {
    if (positionals[1] === "set") {
      const key = requireArg(2,"setting name"), raw = requireArg(3,"setting value");
      const value = key === "reasoning" && raw === "default" ? null : ["workers","steps","port"].includes(key) ? Number(raw) : raw;
      const patch = settingsPatchSchema.parse({[key]:value});
      await workspace.updateSettings(patch);
    } else if (positionals[1]) throw new Error("Use openhack config or openhack config set <key> <value>.");
    return json(await workspace.getSettings());
  }
  throw new Error(`Unknown command: ${safe(command)}. Use openhack --help.`);
}
