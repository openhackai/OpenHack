import { constants } from "node:fs";
import { open } from "node:fs/promises";
import { join } from "node:path";
import { z } from "zod";
import { atomicJson, dataHome, isMissing, privateDirectory, processLock, regularDirectory } from "./files";

export const credentialProviders = ["openai", "anthropic"] as const;
export type CredentialProvider = typeof credentialProviders[number];
export interface ApiCredentials { openai?: string; anthropic?: string }
const providerSchema = z.enum(credentialProviders);
const keySchema = z.string().trim().min(1).max(8192).regex(/^[^\s\u0000-\u001f\u007f]+$/);
const credentialsSchema = z.object({openai: keySchema.optional(), anthropic: keySchema.optional()}).strict();

/** For internal provider setup only. Never put this result in an API response, report, or log. */
export async function readCredentials(home?: string): Promise<ApiCredentials> {
  const directory = dataHome(home);
  try {
    await regularDirectory(directory);
    const file = await open(join(directory, "credentials.json"), constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
    try {
      const info = await file.stat();
      if (!info.isFile()) throw new Error("Saved credentials must be a regular file.");
      if (process.platform !== "win32" && (info.mode & 0o077) !== 0) throw new Error("Saved credentials must have owner-only permissions (chmod 600).");
      if (info.size > 20_000) throw new Error("Saved credentials file is invalid. Reconfigure your API connections.");
      const text = await file.readFile("utf8");
      try { return credentialsSchema.parse(JSON.parse(text)); }
      catch { throw new Error("Saved credentials file is invalid. Reconfigure your API connections."); }
    } finally { await file.close(); }
  } catch (error) { if (isMissing(error)) return {}; throw error; }
}

/** A nonempty environment key wins without mutating either source. */
export function effectiveCredentials(saved: ApiCredentials = {}, env: NodeJS.ProcessEnv = process.env): ApiCredentials {
  return {
    openai: env.OPENAI_API_KEY?.trim() || saved.openai,
    anthropic: env.ANTHROPIC_API_KEY?.trim() || saved.anthropic,
  };
}

export async function saveCredential(home: string | undefined, provider: CredentialProvider, key: string): Promise<void> {
  providerSchema.parse(provider);
  const parsed = keySchema.safeParse(key);
  if (!parsed.success) throw new Error("Enter a nonempty API key without whitespace or control characters.");
  const directory = dataHome(home);
  await privateDirectory(directory);
  const release = await processLock(join(directory, "credentials.lock"));
  try {
    const credentials = await readCredentials(directory);
    await atomicJson(join(directory, "credentials.json"), {...credentials, [provider]: parsed.data}, { durable: true });
  } finally { await release(); }
}

export async function removeCredential(home: string | undefined, provider: CredentialProvider): Promise<void> {
  providerSchema.parse(provider);
  const directory = dataHome(home);
  await privateDirectory(directory);
  const release = await processLock(join(directory, "credentials.lock"));
  try {
    const credentials = await readCredentials(directory);
    delete credentials[provider];
    await atomicJson(join(directory, "credentials.json"), credentials, { durable: true });
  } finally { await release(); }
}
