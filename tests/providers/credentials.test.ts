import { afterEach, describe, expect, test } from "bun:test";
import { chmod, mkdir, mkdtemp, readFile, realpath, rm, stat, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { effectiveCredentials, readCredentials, removeCredential, saveCredential, type ApiCredentials } from "../../src/core/credentials";
import { LocalWorkspace } from "../../src/core/workspace";
import { requireProvider } from "../../src/providers";
import type { AgentClient } from "../../src/harness/openai";
import { withReviews } from "../harness/review-fixture";

const directories: string[] = [];
afterEach(async () => { await Promise.all(directories.splice(0).map(path => rm(path, {recursive: true, force: true}))); });
async function directory() {
  const path = await realpath(await mkdtemp(join(tmpdir(), "openhack-credentials-test-"))); directories.push(path); return path;
}
async function withoutApiEnvironment(action: () => Promise<void>) {
  const original = {openai: process.env.OPENAI_API_KEY, anthropic: process.env.ANTHROPIC_API_KEY};
  delete process.env.OPENAI_API_KEY; delete process.env.ANTHROPIC_API_KEY;
  try { await action(); }
  finally {
    if (original.openai === undefined) delete process.env.OPENAI_API_KEY; else process.env.OPENAI_API_KEY = original.openai;
    if (original.anthropic === undefined) delete process.env.ANTHROPIC_API_KEY; else process.env.ANTHROPIC_API_KEY = original.anthropic;
  }
}
const client: AgentClient = {async run(options) {
  await options.beforeRequest?.(); options.signal.throwIfAborted();
  return options.schema.parse(withReviews(options.name === "Recon" ? {overview: "Test fixture", stack: [], entryPoints: [], trustBoundaries: [], risks: []} : {findings: []}, options.prompt));
}};

describe("private API credentials", () => {
  test("saves keys independently with private permissions and removes only the chosen provider", async () => {
    const home = join(await directory(), "home");
    expect(await readCredentials(home)).toEqual({});
    await saveCredential(home, "openai", "  fixture-openai-key  ");
    await saveCredential(home, "anthropic", "fixture-anthropic-key");
    await saveCredential(home, "openai", "replacement-openai-key");
    expect(await readCredentials(home)).toEqual({openai: "replacement-openai-key", anthropic: "fixture-anthropic-key"});
    expect((await stat(home)).mode & 0o777).toBe(0o700);
    expect((await stat(join(home, "credentials.json"))).mode & 0o777).toBe(0o600);
    await removeCredential(home, "openai");
    expect(await readCredentials(home)).toEqual({anthropic: "fixture-anthropic-key"});
  });
  test("environment overrides are resolved without mutating saved values or the environment", () => {
    const saved = {openai: "saved-openai", anthropic: "saved-anthropic"};
    const env = {OPENAI_API_KEY: "environment-openai", ANTHROPIC_API_KEY: "   "};
    expect(effectiveCredentials(saved, env)).toEqual({openai: "environment-openai", anthropic: "saved-anthropic"});
    expect(saved.openai).toBe("saved-openai"); expect(env.ANTHROPIC_API_KEY).toBe("   ");
  });
  test("workspace settings expose readiness only and saved keys remain scoped to the chosen home", async () => {
    await withoutApiEnvironment(async () => {
      const base = await directory(), homeA = join(base, "a"), homeB = join(base, "b"), project = join(base, "project");
      await mkdir(project); await writeFile(join(project, "app.ts"), "export const value = 1;");
      const seenA: ApiCredentials[] = [], seenB: ApiCredentials[] = [];
      const a = new LocalWorkspace({home: homeA, clientFactory: (_settings, keys) => {seenA.push(keys); return client;}});
      const b = new LocalWorkspace({home: homeB, clientFactory: (_settings, keys) => {seenB.push(keys); return client;}});
      try {
        const configured = await a.configureCredential("openai", "isolated-key-a");
        expect(configured.apiKeyConfigured).toBe(true); expect(configured.providers.find(item => item.id === "openai")!.ready).toBe(true);
        expect(JSON.stringify(configured)).not.toContain("isolated-key-a");
        expect((await b.getSettings()).apiKeyConfigured).toBe(false);
        const scanA = await a.startScan({path: project}); await a.waitForScan(scanA.id);
        const scanB = await b.startScan({path: project}); await b.waitForScan(scanB.id);
        expect(seenA[0]!.openai).toBe("isolated-key-a"); expect(seenB[0]!.openai).toBeUndefined();
        expect(process.env.OPENAI_API_KEY).toBeUndefined();
        const reports = JSON.stringify(await a.listScans()); expect(reports).not.toContain("isolated-key-a");
        await a.updateSettings({workers: 3}); expect(await readFile(join(homeA, "settings.json"), "utf8")).not.toContain("isolated-key-a");
        expect((await a.removeCredential("openai")).apiKeyConfigured).toBe(false);
      } finally { await a.shutdown(); await b.shutdown(); }
    });
  });
  test("provider readiness accepts saved keys and environment keys take precedence at scan construction", async () => {
    await withoutApiEnvironment(async () => {
      await expect(requireProvider("openai", {openai: "saved"})).resolves.toBeUndefined();
      await expect(requireProvider("anthropic", {anthropic: "saved"})).resolves.toBeUndefined();
      await expect(requireProvider("openai", {})).rejects.toThrow("Connect an OpenAI API key");
      const base = await directory(), home = join(base, "home"), project = join(base, "project");
      await mkdir(project); await writeFile(join(project, "app.ts"), "export const value = 1;");
      let seen: ApiCredentials | undefined;
      const app = new LocalWorkspace({home, clientFactory: (_settings, keys) => {seen = keys; return client;}});
      try {
        await app.configureCredential("openai", "saved-key");
        process.env.OPENAI_API_KEY = "env-key";
        const started = await app.startScan({path: project}); await app.waitForScan(started.id);
        expect(seen!.openai).toBe("env-key");
        expect((await readCredentials(home)).openai).toBe("saved-key");
      } finally { await app.shutdown(); }
    });
  });
  test("rejects symlink storage, non-private files, invalid keys and malformed content without echoing keys", async () => {
    const base = await directory(), home = join(base, "home"); await mkdir(home, {mode: 0o700});
    await expect(saveCredential(home, "openai", "secret bad\nkey")).rejects.toThrow("without whitespace");
    const outside = join(base, "outside.json"); await writeFile(outside, '{"openai":"outside-secret"}', {mode: 0o600});
    await symlink(outside, join(home, "credentials.json"));
    await expect(saveCredential(home, "openai", "new-key")).rejects.toThrow();
    expect(await readFile(outside, "utf8")).toContain("outside-secret");
    await rm(join(home, "credentials.json"));
    await writeFile(join(home, "credentials.json"), '{"openai":"private-secret"}', {mode: 0o600}); await chmod(join(home, "credentials.json"), 0o644);
    await expect(readCredentials(home)).rejects.toThrow("owner-only permissions");
    await chmod(join(home, "credentials.json"), 0o600); await writeFile(join(home, "credentials.json"), "private-secret malformed json");
    try { await readCredentials(home); throw new Error("expected failure"); } catch (error) { expect(String(error)).not.toContain("private-secret"); }
  });
});
