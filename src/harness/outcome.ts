import type { Report } from "./types";

export const unfinishedAgentsMessage = "Partial findings are saved; resume retries unfinished tasks.";

/** Recon alone is not a completed source review. Keep total review failures as failed. */
export function hasReviewResults(report: Report): boolean {
  return report.findings.length > 0 || !!report.execution?.tasks.some(task =>
    task.role !== "planner" && (task.state === "complete" || task.state === "limited"));
}

/** Older harness versions marked a finished run failed if any individual agent failed. */
export function normalizeSavedOutcome(report: Report): Report {
  if (report.state === "failed" && report.finishedAt &&
      report.error?.endsWith(unfinishedAgentsMessage) &&
      report.execution?.tasks.some(task => task.state === "failed") && hasReviewResults(report)) {
    return { ...report, state: "partial", phase: "Review finished · some agents failed" };
  }
  return report;
}
