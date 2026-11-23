import { afterEach, expect, test } from "bun:test";
import { mkdir, mkdtemp, readFile, realpath, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { LocalWorkspace } from "../../src/core/workspace";
import { defaultModel } from "../../src/providers";
import { initializeExecution, newReport } from "../../src/harness/scanner";
import { Store } from "../../src/harness/store";
import type { AgentClient } from "../../src/harness/openai";
import type { Settings } from "../../src/shared/contracts";
import { withReviews } from "../harness/review-fixture";

const roots: string[] = [], workspaces: LocalWorkspace[] = [];
afterEach(async () => {
  await Promise.all(workspaces.splice(0).map(workspace => workspace.shutdown()));
  await Promise.all(roots.splice(0).map(root => rm(root, { recursive: true, force: true })));
});
const fakeClient: AgentClient = { async run(options) {
  await options.beforeRequest?.();
  options.signal.throwIfAborted();
  options.onTool("read_file", { path: "app.ts", start: 1 });
  options.onUsage(10, 5);
  options.onFinish?.("completed");
  return options.schema.parse(withReviews(options.name === "Recon"
    ? { overview: "Test fixture", stack: ["TypeScript"], entryPoints: ["app.ts"], trustBoundaries: [], risks: [] }
    : { findings: [] }, options.prompt));
} };

async function fixture() {
  const root = await realpath(await mkdtemp(join(tmpdir(), "openhack-provider-integration-")));
  roots.push(root);
  const home = join(root, "home"), project = join(root, "project");
  await mkdir(project);
  await writeFile(join(project, "app.ts"), "export const value = 1;\n");
  const captured: Settings[] = [];
  const create = () => {
    const workspace = new LocalWorkspace({ home, clientFactory(settings) { captured.push(structuredClone(settings)); return fakeClient; } });
    workspaces.push(workspace);
    return workspace;
  };
  return { home, project, create, captured };
}

test("provider settings persist while changing providers resets model and reasoning defaults", async () => {
  const { home, create } = await fixture();
  const workspace = create();
  await workspace.updateSettings({ provider: "openai", model: "custom-openai-model", reasoning: "high", workers: 2 });
  const switched = await workspace.updateSettings({ provider: "anthropic" });
  expect(switched.provider).toBe("anthropic");
  expect(switched.model).toBe(defaultModel("anthropic"));
  expect(switched.reasoning).toBeUndefined();
  expect(switched.workers).toBe(2);
  await workspace.updateSettings({ provider: "codex", model: "explicit-codex-model", reasoning: "medium" });
  const saved = await create().getSettings();
  expect(saved.provider).toBe("codex");
  expect(saved.model).toBe("explicit-codex-model");
  expect(saved.reasoning).toBe("medium");
  const stored = JSON.parse(await readFile(join(home, "settings.json"), "utf8"));
  expect(stored.providers).toBeUndefined();
  expect(stored.apiKeyConfigured).toBeUndefined();
  expect(stored.home).toBeUndefined();
});

test("per-scan provider overrides use the chosen provider without changing saved settings", async () => {
  const { create, project, captured } = await fixture();
  const workspace = create();
  await workspace.updateSettings({ provider: "openai", model: "saved-openai-model", reasoning: "high" });
  const started = await workspace.startScan({ path: project, provider: "anthropic" });
  expect(started.provider).toBe("anthropic");
  expect(started.model).toBe(defaultModel("anthropic"));
  expect(captured[0]!.provider).toBe("anthropic");
  expect(captured[0]!.reasoning).toBeUndefined();
  expect((await workspace.waitForScan(started.id)).state).toBe("complete");
  expect((await create().getScan(started.id)).provider).toBe("anthropic");
  const settings = await workspace.getSettings();
  expect(settings.provider).toBe("openai");
  expect(settings.model).toBe("saved-openai-model");
  expect(settings.reasoning).toBe("high");
  const explicit = await workspace.startScan({ path: project, provider: "claude-code", model: "explicit-claude-model", reasoning: "low" });
  expect(captured[1]).toMatchObject({ provider: "claude-code", model: "explicit-claude-model", reasoning: "low" });
  expect((await workspace.waitForScan(explicit.id)).state).toBe("complete");
});

test("CLI provider defaults use Opus 4.8 and GPT-6-Sol in new scans and settings", async () => {
  const {create, project, captured} = await fixture();
  const workspace = create();
  for (const [provider, model] of [["claude-code", "claude-opus-4-8"], ["codex", "gpt-6-sol"]] as const) {
    const scan = await workspace.startScan({path: project, provider});
    expect(scan.model).toBe(model);
    expect(captured.at(-1)).toMatchObject({provider, model});
    expect((await workspace.waitForScan(scan.id)).state).toBe("complete");
    expect((await workspace.updateSettings({provider})).model).toBe(model);
  }
});

test("legacy reports resume with OpenAI and their saved model, without current reasoning leaking in", async () => {
  const { home, project, create, captured } = await fixture();
  const workspace = create();
  await workspace.updateSettings({ provider: "anthropic", model: "current-anthropic-model", reasoning: "high" });
  const legacy = newReport(project, "legacy-openai-model");
  initializeExecution(legacy, { steps: 4, workers: 2 });
  legacy.state = "cancelled";
  await new Store(project, home).save(legacy);
  await workspace.resumeScan(legacy.id, { steps: 6 });
  expect(captured[0]).toMatchObject({ provider: "openai", model: "legacy-openai-model", steps: 6, workers: 2 });
  expect(captured[0]!.reasoning).toBeUndefined();
  expect((await workspace.waitForScan(legacy.id)).state).toBe("complete");
  expect((await workspace.getSettings()).provider).toBe("anthropic");
});

test("provider-tagged reports resume their saved provider and reasoning after defaults change", async () => {
  const { home, project, create, captured } = await fixture();
  const workspace = create();
  await workspace.updateSettings({ provider: "openai", model: "current-openai-model", reasoning: "high" });
  const saved = newReport(project, "saved-claude-model");
  saved.provider = "claude-code";
  initializeExecution(saved, { steps: 4, reasoning: "low" });
  saved.state = "cancelled";
  await new Store(project, home).save(saved);
  await workspace.resumeScan(saved.id);
  expect(captured[0]).toMatchObject({ provider: "claude-code", model: "saved-claude-model", reasoning: "low", steps: 4 });
  expect((await workspace.waitForScan(saved.id)).state).toBe("complete");
});
