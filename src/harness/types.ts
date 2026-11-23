import { z } from "zod";
import { providerIds } from "../shared/providers";

export const MAX_WORKERS = 32;
export const severities = ["critical", "high", "medium", "low"] as const;
export const statuses = ["open", "resolved", "dismissed"] as const;
export const candidateSchema = z.object({
  title: z.string().min(1).max(180), severity: z.enum(severities),
  cwe: z.string().regex(/^CWE-\d+$/), file: z.string().min(1).max(500),
  line: z.number().int().positive(), endLine: z.number().int().positive(),
  summary: z.string().min(1).max(5000), evidence: z.string().min(1).max(6000),
  dataFlow: z.string().min(1).max(5000), impact: z.string().min(1).max(3000),
  remediation: z.string().min(1).max(5000),
});
export const reconSchema = z.object({
  overview: z.string().max(6000), stack: z.array(z.string()).max(30),
  entryPoints: z.array(z.string()).max(40), trustBoundaries: z.array(z.string()).max(30),
  risks: z.array(z.string()).max(12),
});
export const huntSchema = z.object({ findings: z.array(candidateSchema).max(12) });
export const sourceReviewSchema = huntSchema.extend({
  reviews: z.array(z.object({ key: z.string(), status: z.enum(["reviewed", "unreviewed"]), reason: z.string().min(1).max(1500) })).max(32),
});
export const verdictSchema = z.object({
  verdict: z.enum(["validated", "rejected", "uncertain"]),
  reason: z.string().min(1).max(5000), severity: z.enum(severities),
});
export const findingSchema = candidateSchema.extend({
  id: z.string(), validation: z.enum(["pending", "validated", "rejected", "uncertain"]), validationReason: z.string(),
});
export const triageSchema = z.object({ status: z.enum(statuses), note: z.string().max(10000), updatedAt: z.string() });
export const agentEventSchema = z.object({ at: z.string(), actor: z.string(), text: z.string(), tone: z.enum(["success", "warning", "danger"]).optional() });
export const sourceSegmentSchema = z.object({ path: z.string(), from: z.number(), to: z.number(), start: z.number(), end: z.number() });
export type SourceSegment = z.infer<typeof sourceSegmentSchema>;
export const agentTaskSchema = z.object({
  id: z.string(), role: z.enum(["planner", "research", "advocate", "validator", "judge"]), title: z.string(), scope: z.array(z.string()),
  state: z.enum(["queued", "running", "complete", "failed", "blocked", "limited"]), elapsed: z.number(), duration: z.number(), files: z.number(),
  candidate: z.string().optional(), results: z.array(z.number()), trace: z.array(agentEventSchema),
  instruction: z.string().optional(), findingIds: z.array(z.string()).optional(), verdict: verdictSchema.optional(),
  segments: z.array(sourceSegmentSchema).optional(), stopReason: z.enum(["completed", "tool_limit"]).optional(),
  reviews: sourceReviewSchema.shape.reviews.optional(), recoveryOf: z.string().optional(), recovered: z.boolean().optional(),
});
export type AgentTask = z.infer<typeof agentTaskSchema>;
export type AgentEvent = z.infer<typeof agentEventSchema>;
export const executionSchema = z.object({
  version: z.literal(1), workers: z.number().int().min(1).max(MAX_WORKERS), steps: z.number().int().min(1).max(30),
  context: z.string(), reasoning: z.enum(["none", "minimal", "low", "medium", "high", "xhigh"]).optional(),
  sourceHash: z.string().optional(), paused: z.boolean(), elapsed: z.number(), cached: z.number(),
  tasks: z.array(agentTaskSchema), events: z.array(agentEventSchema),
  planVersion: z.literal(2).optional(),
});
export const reportSchema = z.object({
  provider: z.enum(providerIds).optional(),
  version: z.literal(1), id: z.string(), project: z.string(), root: z.string(), model: z.string(),
  startedAt: z.string(), finishedAt: z.string().optional(),
  state: z.enum(["running", "complete", "cancelled", "failed", "partial"]), phase: z.string(), error: z.string().optional(),
  inventory: z.object({ files: z.number(), bytes: z.number(), skipped: z.number(), limited: z.boolean(), omissions: z.array(z.object({path:z.string(),reason:z.string()})).optional() }),
  readFiles: z.array(z.string()), recon: reconSchema.optional(), findings: z.array(findingSchema),
  usage: z.object({ input: z.number(), output: z.number(), requests: z.number() }),
  events: z.array(z.object({ at: z.string(), message: z.string() })),
  execution: executionSchema.optional(),
});
export type Candidate = z.infer<typeof candidateSchema>;
export type Finding = z.infer<typeof findingSchema>;
export type Report = z.infer<typeof reportSchema>;
export type Triage = z.infer<typeof triageSchema>;
export type Status = typeof statuses[number];
export type Severity = typeof severities[number];
export type TriageMap = Record<string, Triage>;
export const rank = (s: Severity) => severities.indexOf(s);
export const stateFor = (id: string, triage: TriageMap): Triage => triage[id] ?? { status: "open", note: "", updatedAt: "" };
