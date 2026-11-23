import { afterEach, expect, test } from "bun:test";
import { mkdtemp, realpath, rm, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { LocalWorkspace } from "../src/core/workspace";
import { effectiveCredentials, readCredentials } from "../src/core/credentials";
import { startDashboard } from "../src/server";
import type { ProviderStatus } from "../src/shared/providers";
import type { SettingsResponse } from "../src/shared/contracts";

const roots: string[] = [], servers: Bun.Server<void>[] = [], workspaces: LocalWorkspace[] = [];
afterEach(async () => {
  await Promise.all(servers.splice(0).map(server => server.stop(true)));
  await Promise.all(workspaces.splice(0).map(workspace => workspace.shutdown()));
  await Promise.all(roots.splice(0).map(root => rm(root, { recursive: true, force: true })));
});

async function fixture() {
  const home = await realpath(await mkdtemp(join(tmpdir(), "openhack-provider-api-")));
  roots.push(home);
  const workspace = new LocalWorkspace({ home });
  workspaces.push(workspace);
  const server = startDashboard({ workspace, port: 0, devOrigins: ["http://127.0.0.1:5173"] });
  servers.push(server);
  const url = `http://127.0.0.1:${server.port}`;
  const request = (path: string, method: string, body: unknown = {}, extraHeaders: Record<string, string> = {}) => fetch(`${url}${path}`, {
    method, headers: { "Content-Type": "application/json", "X-OpenHack-Client": "dashboard", ...extraHeaders }, body: JSON.stringify(body),
  });
  return { home, workspace, url, request };
}

async function withApiEnvironment(action: () => Promise<void>) {
  const original = { openai: process.env.OPENAI_API_KEY, anthropic: process.env.ANTHROPIC_API_KEY };
  delete process.env.OPENAI_API_KEY;
  delete process.env.ANTHROPIC_API_KEY;
  try { await action(); }
  finally {
    if (original.openai === undefined) delete process.env.OPENAI_API_KEY; else process.env.OPENAI_API_KEY = original.openai;
    if (original.anthropic === undefined) delete process.env.ANTHROPIC_API_KEY; else process.env.ANTHROPIC_API_KEY = original.anthropic;
  }
}

function status(providers: ProviderStatus[], id: ProviderStatus["id"]) {
  const provider = providers.find(provider => provider.id === id);
  if (!provider) throw new Error("Missing provider status");
  return provider;
}

test("dashboard saves, replaces, and removes private provider keys without returning them", async () => {
  await withApiEnvironment(async () => {
    const app = await fixture();
    const firstKey = "fixture-private-openai-first", secondKey = "fixture-private-openai-replacement", otherKey = "fixture-private-anthropic";
    for (const [provider, key] of [["openai", firstKey], ["anthropic", otherKey], ["openai", secondKey]]) {
      const response = await app.request(`/api/providers/${provider}/credentials`, "POST", { key });
      expect(response.status).toBe(200);
      const text = await response.text();
      for (const secret of [firstKey, secondKey, otherKey]) expect(text).not.toContain(secret);
      const result = JSON.parse(text) as SettingsResponse;
      expect(status(result.settings.providers, provider as "openai" | "anthropic")).toMatchObject({ ready: true, credentialSource: "saved", savedKeyConfigured: true });
    }
    expect(await readCredentials(app.home)).toEqual({ openai: secondKey, anthropic: otherKey });
    expect((await stat(join(app.home, "credentials.json"))).mode & 0o777).toBe(0o600);
    const fresh = new LocalWorkspace({ home: app.home });
    workspaces.push(fresh);
    expect(status((await fresh.getSettings()).providers, "openai").credentialSource).toBe("saved");
    const removed = await app.request("/api/providers/openai/credentials", "DELETE");
    expect(removed.status).toBe(200);
    const removedResult = await removed.json() as SettingsResponse;
    expect(status(removedResult.settings.providers, "openai")).toMatchObject({ ready: false, savedKeyConfigured: false });
    expect(status(removedResult.settings.providers, "openai").credentialSource).toBeUndefined();
    expect(await readCredentials(app.home)).toEqual({ anthropic: otherKey });
    expect((await fetch(`${app.url}/api/providers/openai/credentials`)).status).toBe(405);
    expect((await app.request("/api/providers/codex/credentials", "POST", { key: firstKey })).status).toBe(404);
    expect((await app.request("/api/providers/claude-code/credentials", "DELETE")).status).toBe(404);
  });
});

test("metadata distinguishes the effective environment key from an independently saved key", async () => {
  await withApiEnvironment(async () => {
    const app = await fixture();
    process.env.OPENAI_API_KEY = "fixture-environment-secret";
    const saved = await app.request("/api/providers/openai/credentials", "POST", { key: "fixture-saved-secret" });
    const text = await saved.text();
    expect(text).not.toContain("fixture-environment-secret");
    expect(text).not.toContain("fixture-saved-secret");
    expect(status((JSON.parse(text) as SettingsResponse).settings.providers, "openai")).toMatchObject({ ready: true, credentialSource: "environment", savedKeyConfigured: true });
    expect(effectiveCredentials(await readCredentials(app.home)).openai).toBe("fixture-environment-secret");
    const removed = await app.request("/api/providers/openai/credentials", "DELETE");
    expect(status((await removed.json() as SettingsResponse).settings.providers, "openai")).toMatchObject({ ready: true, credentialSource: "environment", savedKeyConfigured: false });
    expect(process.env.OPENAI_API_KEY).toBe("fixture-environment-secret");
  });
});

test("invalid credential payloads have fixed errors and never reflect values or field names", async () => {
  const app = await fixture();
  const marker = "DO_NOT_REFLECT_PRIVATE_INPUT";
  for (const body of [{ key: `${marker} with spaces` }, { key: `${marker}\0` }, { key: "a".repeat(8193) }, { key: marker, [marker]: true }, { key: null }, {}, { key: "" }]) {
    const response = await app.request("/api/providers/openai/credentials", "POST", body);
    expect(response.status).toBe(400);
    expect(await response.text()).not.toContain(marker);
  }
  for (const path of ["/api/providers/openai/credentials", "/api/providers/refresh"]) {
    const response = await app.request(path, path.endsWith("refresh") ? "POST" : "DELETE", { [marker]: marker });
    expect(response.status).toBe(400);
    expect(await response.text()).not.toContain(marker);
  }
  expect(await readCredentials(app.home)).toEqual({});
  app.workspace.configureCredential = async () => { throw new Error(`Failure echoed ${marker}`); };
  const failed = await app.request("/api/providers/openai/credentials", "POST", { key: marker });
  expect(failed.status).toBe(500);
  expect(await failed.text()).not.toContain(marker);
});

test("cross-site and unmarked credential changes are rejected before any storage mutation", async () => {
  const app = await fixture();
  await app.workspace.configureCredential("openai", "fixture-original-key");
  for (const method of ["POST", "DELETE"]) {
    const body = method === "POST" ? { key: "fixture-rejected-key" } : {};
    const response = await app.request("/api/providers/openai/credentials", method, body, { Origin: "https://untrusted.example" });
    expect(response.status).toBe(403);
    expect((await fetch(`${app.url}/api/providers/openai/credentials`, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).status).toBe(403);
  }
  expect((await app.request("/api/providers/refresh", "POST", {}, { Origin: "https://untrusted.example" })).status).toBe(403);
  expect(await readCredentials(app.home)).toEqual({ openai: "fixture-original-key" });
  const preflight = await fetch(`${app.url}/api/providers/openai/credentials`, { method: "OPTIONS", headers: { Origin: "http://127.0.0.1:5173", "Access-Control-Request-Method": "DELETE" } });
  expect(preflight.status).toBe(204);
  expect(preflight.headers.get("Access-Control-Allow-Methods")).toContain("DELETE");
});

test("provider refresh includes saved-key metadata and invokes a fresh probe on each request", async () => {
  await withApiEnvironment(async () => {
    const app = await fixture();
    await app.workspace.configureCredential("anthropic", "fixture-refresh-private-key");
    const response = await app.request("/api/providers/refresh", "POST");
    expect(response.status).toBe(200);
    const text = await response.text();
    expect(text).not.toContain("fixture-refresh-private-key");
    const providers = (JSON.parse(text) as { providers: ProviderStatus[] }).providers;
    expect(status(providers, "anthropic")).toMatchObject({ ready: true, credentialSource: "saved", savedKeyConfigured: true });
    expect(status(providers, "codex").credentialSource).toBeUndefined();
    expect(status(providers, "claude-code").savedKeyConfigured).toBeUndefined();
    let probes = 0;
    app.workspace.refreshProviders = async () => { probes++; return providers; };
    await app.request("/api/providers/refresh", "POST");
    await app.request("/api/providers/refresh", "POST");
    expect(probes).toBe(2);
  });
});
