export const providerIds = ["openai", "anthropic", "codex", "claude-code"] as const;
export type ProviderId = typeof providerIds[number];
export const providerDefinitions: Record<ProviderId, {label: string; defaultModel: string; description: string}> = {
  openai: {label: "OpenAI API", defaultModel: "gpt-5-mini", description: "Uses your OpenAI API key. Billed to your OpenAI API account."},
  anthropic: {label: "Anthropic API", defaultModel: "claude-sonnet-4-6", description: "Uses your Anthropic API key. Billed to your Anthropic API account."},
  codex: {label: "Codex login", defaultModel: "gpt-6-sol", description: "Uses your installed Codex CLI and its existing login. ChatGPT plan limits or API billing apply based on that login."},
  "claude-code": {label: "Claude Code login", defaultModel: "claude-opus-4-8", description: "Uses your installed Claude Code CLI and its Claude login. Your plan's usage limits apply."},
};
export interface ProviderStatus {
  id: ProviderId;
  ready: boolean;
  detail: string;
  credentialSource?: "environment" | "saved";
  savedKeyConfigured?: boolean;
}
