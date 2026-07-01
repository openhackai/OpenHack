"""
Provider registry — connect OpenHack to any model, any provider.

OpenHack ships with its own hosted provider (Kimi-K2.5, no setup, free credits)
as the default. But power users want to bring their own key and model — exactly
like OpenCode. Because our LLM client talks the OpenAI wire format over a
configurable base URL, any OpenAI-compatible endpoint drops straight in: OpenAI,
Anthropic (OpenAI-compat endpoint), OpenRouter, Groq, DeepSeek, Together, or a
local Ollama.

Selection order for a run:
  1. `llm_provider` setting (env OPENHACK_LLM_PROVIDER, ~/.openhack/config, or .env)
  2. that provider's spec here gives the base URL, key env var and default model
  3. the model can be overridden per-call or via `openhack_model_id`

This module is pure data + resolution; the LLM client consumes it.
"""

import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    label: str
    base_url: str
    api_key_env: str
    default_model: str
    supports_prompt_cache: bool = False
    # model id -> {"input": $/1M tokens, "output": $/1M tokens}
    pricing: dict = field(default_factory=dict)
    # A dummy key value for keyless local servers (e.g. Ollama).
    keyless_default: Optional[str] = None


# The OpenHack provider is special-cased in the client (its base URL/key come
# from the existing settings + device-login flow), so it isn't listed here with
# a hardcoded base URL. Everything below is bring-your-own-key.
PROVIDERS: dict[str, ProviderSpec] = {
    "openai": ProviderSpec(
        name="openai",
        label="OpenAI",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        default_model="gpt-4.1",
        supports_prompt_cache=True,
    ),
    "anthropic": ProviderSpec(
        name="anthropic",
        label="Anthropic (Claude)",
        base_url="https://api.anthropic.com/v1",
        api_key_env="ANTHROPIC_API_KEY",
        default_model="claude-sonnet-5",
    ),
    "openrouter": ProviderSpec(
        name="openrouter",
        label="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        default_model="anthropic/claude-sonnet-5",
    ),
    "groq": ProviderSpec(
        name="groq",
        label="Groq",
        base_url="https://api.groq.com/openai/v1",
        api_key_env="GROQ_API_KEY",
        default_model="llama-3.3-70b-versatile",
    ),
    "deepseek": ProviderSpec(
        name="deepseek",
        label="DeepSeek",
        base_url="https://api.deepseek.com/v1",
        api_key_env="DEEPSEEK_API_KEY",
        default_model="deepseek-chat",
    ),
    "together": ProviderSpec(
        name="together",
        label="Together AI",
        base_url="https://api.together.xyz/v1",
        api_key_env="TOGETHER_API_KEY",
        default_model="meta-llama/Llama-3.3-70B-Instruct-Turbo",
    ),
    "ollama": ProviderSpec(
        name="ollama",
        label="Ollama (local)",
        base_url="http://localhost:11434/v1",
        api_key_env="OLLAMA_API_KEY",
        default_model="llama3.1",
        keyless_default="ollama",
    ),
}


@dataclass
class ResolvedProvider:
    name: str
    base_url: str
    api_key: Optional[str]
    model: str
    supports_prompt_cache: bool
    pricing: dict
    missing_key_env: Optional[str] = None  # set when the key is absent


def is_known(name: str) -> bool:
    return name == "openhack" or name in PROVIDERS


def list_providers() -> list[str]:
    return ["openhack", *PROVIDERS.keys()]


def resolve(name: str, model: Optional[str] = None) -> Optional[ResolvedProvider]:
    """Resolve a non-OpenHack provider to concrete connection params.

    Returns None for 'openhack' (the client handles that via settings) or an
    unknown provider name. Reads the API key and optional base-URL override from
    the environment. A missing key is reported (not raised) so callers can give a
    friendly message.
    """
    if name == "openhack" or name not in PROVIDERS:
        return None

    spec = PROVIDERS[name]
    # Allow a per-provider base URL override, e.g. OPENAI_BASE_URL, OLLAMA_BASE_URL.
    base_url = os.environ.get(f"{name.upper()}_BASE_URL", spec.base_url)
    api_key = os.environ.get(spec.api_key_env) or spec.keyless_default
    return ResolvedProvider(
        name=name,
        base_url=base_url,
        api_key=api_key,
        model=model or os.environ.get(f"{name.upper()}_MODEL") or spec.default_model,
        supports_prompt_cache=spec.supports_prompt_cache,
        pricing=spec.pricing,
        missing_key_env=None if api_key else spec.api_key_env,
    )
