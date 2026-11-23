import { z } from "zod";
import { providerIds, type ProviderStatus } from "./providers";
import { reasoningEfforts } from "../harness/openai";
import type { Finding, Report, Status } from "../harness/types";
export type { Finding, Report, Severity, Status } from "../harness/types";

export const settingsSchema = z.object({
  provider: z.enum(providerIds),
  model: z.string().trim().min(1).max(200),
  workers: z.number().int().min(1).max(32),
  steps: z.number().int().min(1).max(30),
  reasoning: z.enum(reasoningEfforts).optional(),
  context: z.string().max(12000),
  port: z.number().int().min(1).max(65535),
});
export type Settings = z.infer<typeof settingsSchema>;
export interface SettingsView extends Settings { home: string; apiKeyConfigured: boolean; providers: ProviderStatus[] }
export const settingsPatchSchema = settingsSchema.partial().extend({reasoning: z.enum(reasoningEfforts).nullable().optional()}).strict();
export const startScanSchema = settingsSchema.omit({port: true}).partial().extend({path: z.string().trim().min(1).max(4096)}).strict();
export type StartScanInput = z.infer<typeof startScanSchema>;
export const triagePatchSchema = z.object({status: z.enum(["open", "resolved", "dismissed"]).optional(), note: z.string().max(10000).optional()}).strict().refine(value => value.status !== undefined || value.note !== undefined, "Provide status or note.");
export type TriagePatch = z.infer<typeof triagePatchSchema>;
export interface FindingRecord extends Finding { key: string; scanId: string; project: string; root: string; status: Status; note: string; updatedAt: string }
export interface ScansResponse { scans: Report[] }
export interface ScanResponse { scan: Report }
export interface FindingsResponse { findings: FindingRecord[] }
export interface SettingsResponse { settings: SettingsView }
export interface ErrorResponse { error: string }
export interface DirectoryEntry { name: string; path: string }
export interface DirectoryListing { path: string; parent: string | null; directories: DirectoryEntry[]; truncated: boolean }
