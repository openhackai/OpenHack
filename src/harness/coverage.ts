import { basename } from "node:path";
import type { Project } from "./project";
import type { Report, SourceSegment } from "./types";

export function sourcePriority(path: string) {
  if (/(^|\/)(test[s]?|__tests__|docs|fixtures)(\/|\.)/.test(path) || /\.(md|test\.[jt]sx?|spec\.[jt]sx?)$/.test(path)) return 3;
  if (/^(package\.json|pyproject\.toml|go\.mod|Cargo\.toml|Gemfile|composer\.json|.*config\.[cm]?[jt]s)$/.test(basename(path))) return 0;
  if (/(^|\/)(auth|middleware|api|routes|controllers|server|services|lib)(\/|\.)/.test(path)) return 1;
  return 2;
}
export const orderedPaths = (project: Project) => [...project.files.keys()].sort((a, b) => sourcePriority(a) - sourcePriority(b) || a.localeCompare(b));
export const segmentKey = (s: SourceSegment) => `${s.path}@${s.from}:${s.to}`;

/** Every source character has an assignment. Long files are split, never silently clipped. */
export function planSource(project: Project): SourceSegment[][] {
  const batches: SourceSegment[][] = [];
  let batch: SourceSegment[] = [], size = 0;
  for (const path of orderedPaths(project)) {
    const source = project.files.get(path)!;
    let from = 0;
    do {
      let to = Math.min(source.length, from + 16000);
      if (to < source.length) { const newline = source.lastIndexOf("\n", to); if (newline > from + 8000) to = newline + 1; }
      const segment = { path, from, to, start: source.slice(0, from).split("\n").length, end: source.slice(0, to).split("\n").length };
      if (batch.length && (size + to - from > 32000 || batch.length >= 6)) { batches.push(batch); batch = []; size = 0; }
      batch.push(segment); size += to - from; from = to;
    } while (from < source.length);
  }
  if (batch.length) batches.push(batch);
  return batches;
}
export function segmentPrompt(project: Project, segments: SourceSegment[]): string {
  return segments.map(s => {
    const source = project.files.get(s.path)!.slice(s.from, s.to);
    return `SOURCE ${s.path} · lines ${s.start}-${s.end} · character range ${s.from}-${s.to}\nREVIEW KEY ${segmentKey(s)}\n` + source.split("\n").map((line, i) => `${s.start + i}: ${line}`).join("\n") + "\nEND SOURCE";
  }).join("\n\n");
}

export function coverageSummary(report: Report) {
  const e = report.execution;
  // Recovery agents own their parent's remaining objectives. Counting both the
  // original stop and its follow-ups reports stale budgets and duplicates gaps.
  const recoveredParents = new Set(e?.tasks.map(t => t.recoveryOf).filter(Boolean));
  const leaves = e?.tasks.filter(t => !t.recovered && !recoveredParents.has(t.id)) ?? [];
  const hitBudget = (t: (typeof leaves)[number]) => t.stopReason === "tool_limit" || (!e?.planVersion && t.trace.some(event => event.text.endsWith("· completing")));
  const limited = leaves.filter(t => t.state === "limited" || hitBudget(t)).length;
  const assignments = e?.tasks.filter(t => t.role === "research" && t.segments?.length && !t.recoveryOf) ?? [];
  const planned = assignments.length, completed = assignments.filter(t => t.state === "complete").length;
  const reasons: string[] = [];
  if (report.inventory.limited) reasons.push("Source snapshot is incomplete." + (report.inventory.omissions?.length ? " Omitted: " + report.inventory.omissions.slice(0,3).map(o => `${o.path} (${o.reason})`).join("; ") : " A size/file limit or unreadable source caused omissions."));
  const budgetLimited = leaves.filter(hitBudget).length;
  if (budgetLimited) reasons.push(`${budgetLimited} agent(s) reached the tool-round limit; their review is incomplete.`);
  if (limited > budgetLimited) reasons.push(`${limited - budgetLimited} agent(s) left source objectives unreviewed.`);
  if (e?.planVersion === 2 && planned && completed < planned) reasons.push(`${completed}/${planned} source assignments completed.`);
  if (e?.planVersion !== 2 && report.state === "complete" && report.readFiles.length < report.inventory.files) reasons.push(`Legacy scan opened only ${report.readFiles.length}/${report.inventory.files} files and had no repository-wide coverage plan.`);
  const partial = report.state === "partial" || (report.state === "complete" && reasons.length > 0);
  return { planned, completed, limited, partial, reasons };
}
