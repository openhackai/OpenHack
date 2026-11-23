import { effectiveCredentials, type ApiCredentials } from "../core/credentials";
import { OpenAIClient, type AgentClient } from "../harness/openai";
import type { Settings } from "../shared/contracts";
import { providerDefinitions, type ProviderId, type ProviderStatus } from "../shared/providers";
import { AnthropicClient } from "./anthropic";
import { ClaudeCodeClient, CodexClient, probeSubscriptionLogin } from "./subscription";

export function defaultModel(provider: ProviderId): string {
  if (provider === "openai") return process.env.OPENAI_MODEL || providerDefinitions.openai.defaultModel;
  if (provider === "anthropic") return process.env.ANTHROPIC_MODEL || providerDefinitions.anthropic.defaultModel;
  return providerDefinitions[provider].defaultModel;
}

export function createClient(settings: Settings, saved: ApiCredentials = {}): AgentClient {
  const credentials = effectiveCredentials(saved);
  switch (settings.provider) {
    case "openai": return new OpenAIClient(credentials.openai ?? "", settings.model, undefined, undefined, settings.reasoning);
    case "anthropic": return new AnthropicClient(credentials.anthropic ?? "", settings.model, undefined, settings.reasoning);
    case "codex": return new CodexClient(settings.model, settings.reasoning);
    case "claude-code": return new ClaudeCodeClient(settings.model, settings.reasoning);
  }
}

let loginCache: {expires: number; pending: Promise<ProviderStatus[]>} | undefined;
async function subscriptionStatuses(refresh: boolean): Promise<ProviderStatus[]> {
  if (refresh || !loginCache || loginCache.expires < Date.now()) {
    const pending = Promise.all((["codex", "claude-code"] as const).map(async id => {
      const probe = await probeSubscriptionLogin(id === "codex" ? "codex" : "claude");
      return {id, ready: probe.ready, detail: probe.message};
    }));
    loginCache = {expires: Date.now() + 15_000, pending};
    pending.catch(() => { if (loginCache?.pending === pending) loginCache = undefined; });
  }
  return loginCache.pending;
}

export async function providerStatuses(refresh = false, saved: ApiCredentials = {}): Promise<ProviderStatus[]> {
  const credentials = effectiveCredentials(saved);
  const detail = (provider: "openai" | "anthropic", variable: string): string => process.env[variable]?.trim()
    ? `${variable} is configured in the environment.`
    : credentials[provider] ? "An API key is saved in this OpenHack workspace." : "Connect an API key in OpenHack setup or set the provider environment variable.";
  const apiStatus = (id: "openai" | "anthropic", variable: string): ProviderStatus => {
    const credentialSource = process.env[variable]?.trim() ? "environment" : saved[id] ? "saved" : undefined;
    return { id, ready: !!credentials[id], detail: detail(id, variable), savedKeyConfigured: !!saved[id], ...(credentialSource ? { credentialSource } : {}) };
  };
  return [
    apiStatus("openai", "OPENAI_API_KEY"),
    apiStatus("anthropic", "ANTHROPIC_API_KEY"),
    ...await subscriptionStatuses(refresh),
  ];
}

export async function requireProvider(provider: ProviderId, saved: ApiCredentials = {}): Promise<void> {
  if (provider === "openai" || provider === "anthropic") {
    if (!effectiveCredentials(saved)[provider]) throw new Error(`Connect an ${provider === "openai" ? "OpenAI" : "Anthropic"} API key in OpenHack setup or set ${provider === "openai" ? "OPENAI_API_KEY" : "ANTHROPIC_API_KEY"}.`);
    return;
  }
  const status = await probeSubscriptionLogin(provider === "codex" ? "codex" : "claude");
  if (!status.ready) throw new Error(status.message);
}
