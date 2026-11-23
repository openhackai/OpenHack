import { clearLine, cursorTo } from "node:readline";
import type { Report } from "../harness/types";
import { providerDefinitions } from "../shared/providers";
import { safe } from "./display";

const frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
const elapsedLabel = (milliseconds: number) => {
  const seconds = Math.max(0, Math.floor(milliseconds / 1000));
  return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
};

export function progressSummary(report: Report | undefined, elapsed: number, cancelling = false): string {
  if (cancelling) return `Stopping scan · saving checkpoint · ${elapsedLabel(elapsed)}`;
  if (!report) return `Preparing scan · ${elapsedLabel(elapsed)}`;
  const tasks = report.execution?.tasks ?? [];
  const active = tasks.filter(task => task.state === "running").length;
  const done = tasks.filter(task => task.state === "complete").length;
  return `${safe(report.phase)} · ${elapsedLabel(elapsed)} · ${active} active · ${done}/${tasks.length} tasks · ${report.readFiles.length}/${report.inventory.files} files read · ${report.findings.length} findings · ${report.usage.requests} requests`;
}

/** One live terminal line; plain logs for pipes. Never changes raw mode or signal handlers. */
export function startScanProgress(read: (id: string) => Promise<Report>) {
  const output = process.stderr;
  const live = !!output.isTTY && process.env.TERM !== "dumb";
  const started = Date.now();
  let report: Report | undefined;
  let frame = 0, stopped = false, polling = false, cancelling = false;
  let lastEvent: string | undefined, lastPhase: string | undefined, lastLog = 0;
  const erase = () => { if (live) { cursorTo(output, 0); clearLine(output, 0); } };
  const line = (text: string) => { erase(); output.write(`${safe(text)}\n`); };
  const draw = () => {
    if (stopped) return;
    const summary = progressSummary(report, Date.now() - started, cancelling);
    if (live) {
      erase();
      const width = Math.max(1, (output.columns || 100) - 1);
      const text = `${frames[frame++ % frames.length]} ${summary}`;
      output.write(text.length > width ? `${text.slice(0, Math.max(0, width - 1))}…` : text);
    } else if (Date.now() - lastLog >= 15_000 || report?.phase !== lastPhase) {
      line(summary); lastLog = Date.now(); lastPhase = report?.phase;
    }
  };
  const update = (next: Report) => {
    report = next;
    const latest = next.events.at(-1);
    const key = latest && `${latest.at}:${latest.message}`;
    if (latest && key !== lastEvent) {
      lastEvent = key;
      line(`  ${elapsedLabel(Date.now() - started)}  ${latest.message}`);
    }
    draw();
  };
  draw();
  const animation = setInterval(draw, live ? 100 : 1000);
  const poll = setInterval(() => {
    if (!report || polling || stopped) return;
    polling = true;
    void read(report.id).then(next => { if (!stopped) update(next); }).catch(() => {}).finally(() => { polling = false; });
  }, 750);
  return {
    attach(next: Report) {
      line(`Scanning ${next.root}`);
      line(`Provider: ${providerDefinitions[next.provider ?? "openai"].label} · Model: ${next.model}`);
      line(`Scan: ${next.id} · Ctrl+C to stop and save`);
      update(next);
    },
    cancel() { cancelling = true; lastLog = 0; draw(); },
    stop() {
      if (stopped) return;
      stopped = true; clearInterval(animation); clearInterval(poll); erase();
    },
  };
}
