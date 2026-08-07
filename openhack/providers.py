"""Provider registry and Models.dev-backed discovery.

OpenHack combines a short curated list with Models.dev. The curated providers
are instant and work offline; the live registry supplies the long tail and the
current model catalog.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from openhack.model_catalog import (
    EXCLUDED_PROVIDER_IDS,
    discover_compatible_providers,
    merge_models,
    provider_from_models_dev,
    rank_model_ids,
)
from openhack.provider_auth import (
    OPENAI_CODEX_BASE_URL,
    OPENAI_SUBSCRIPTION_MODELS,
    all_credentials,
    get_credential,
)


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    label: str
    base_url: str
    api_key_env: str
    default_model: str
    supports_prompt_cache: bool = False
    pricing: dict = field(default_factory=dict)
    keyless_default: Optional[str] = None
    hint: str = ""
    required_env: tuple[str, ...] = ()


# Curated providers appear first. Models.dev providers follow alphabetically.
PROVIDERS: dict[str, ProviderSpec] = {
    "openai": ProviderSpec(
        "openai", "OpenAI", "https://api.openai.com/v1",
        "OPENAI_API_KEY", "gpt-5.6-sol", True,
        hint="API key or ChatGPT Plus/Pro subscription",
    ),
    "anthropic": ProviderSpec(
        "anthropic", "Anthropic", "https://api.anthropic.com/v1",
        "ANTHROPIC_API_KEY", "claude-sonnet-5",
        hint="Claude models through Anthropic's OpenAI-compatible endpoint",
    ),
    "openrouter": ProviderSpec(
        "openrouter", "OpenRouter", "https://openrouter.ai/api/v1",
        "OPENROUTER_API_KEY", "anthropic/claude-sonnet-5", True,
        hint="Hundreds of models through one account",
    ),
    "google": ProviderSpec(
        "google", "Google AI Studio", "https://generativelanguage.googleapis.com/v1beta/openai",
        "GEMINI_API_KEY", "gemini-3.1-pro", True,
    ),
    "vercel": ProviderSpec(
        "vercel", "Vercel AI Gateway", "https://ai-gateway.vercel.sh/v1",
        "AI_GATEWAY_API_KEY", "anthropic/claude-sonnet-4.6", True,
        hint="Hundreds of models through Vercel AI Gateway",
    ),
    "cloudflare-workers-ai": ProviderSpec(
        "cloudflare-workers-ai", "Cloudflare Workers AI",
        "https://api.cloudflare.com/client/v4/accounts/"
        "${CLOUDFLARE_ACCOUNT_ID}/ai/v1",
        "CLOUDFLARE_API_KEY", "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
        hint="Requires CLOUDFLARE_ACCOUNT_ID",
        required_env=("CLOUDFLARE_ACCOUNT_ID",),
    ),
    "xai": ProviderSpec(
        "xai", "xAI", "https://api.x.ai/v1",
        "XAI_API_KEY", "grok-4.5", True,
    ),
    "groq": ProviderSpec(
        "groq", "Groq", "https://api.groq.com/openai/v1",
        "GROQ_API_KEY", "llama-3.3-70b-versatile",
    ),
    "deepseek": ProviderSpec(
        "deepseek", "DeepSeek", "https://api.deepseek.com",
        "DEEPSEEK_API_KEY", "deepseek-chat", True,
    ),
    "mistral": ProviderSpec(
        "mistral", "Mistral", "https://api.mistral.ai/v1",
        "MISTRAL_API_KEY", "mistral-large-latest",
    ),
    "together": ProviderSpec(
        "together", "Together AI", "https://api.together.xyz/v1",
        "TOGETHER_API_KEY", "meta-llama/Llama-3.3-70B-Instruct-Turbo",
    ),
    "fireworks-ai": ProviderSpec(
        "fireworks-ai", "Fireworks AI", "https://api.fireworks.ai/inference/v1",
        "FIREWORKS_API_KEY", "accounts/fireworks/models/glm-5p2",
    ),
    "cerebras": ProviderSpec(
        "cerebras", "Cerebras", "https://api.cerebras.ai/v1",
        "CEREBRAS_API_KEY", "llama-3.3-70b",
    ),
    "deepinfra": ProviderSpec(
        "deepinfra", "DeepInfra", "https://api.deepinfra.com/v1/openai",
        "DEEPINFRA_API_KEY", "meta-llama/Llama-3.3-70B-Instruct",
    ),
    "nebius": ProviderSpec(
        "nebius", "Nebius Token Factory", "https://api.tokenfactory.nebius.com/v1",
        "NEBIUS_API_KEY", "meta-llama/Llama-3.3-70B-Instruct",
    ),
    "nvidia": ProviderSpec(
        "nvidia", "NVIDIA NIM", "https://integrate.api.nvidia.com/v1",
        "NVIDIA_API_KEY", "meta/llama-3.3-70b-instruct",
    ),
    "moonshotai": ProviderSpec(
        "moonshotai", "Moonshot AI", "https://api.moonshot.ai/v1",
        "MOONSHOT_API_KEY", "kimi-k2.5",
    ),
    "azure": ProviderSpec(
        "azure", "Azure OpenAI", "${AZURE_OPENAI_BASE_URL}",
        "AZURE_OPENAI_API_KEY", "gpt-5-mini", True,
        hint="Requires the full v1 URL in AZURE_OPENAI_BASE_URL",
        required_env=("AZURE_OPENAI_BASE_URL",),
    ),
    "amazon-bedrock": ProviderSpec(
        "amazon-bedrock", "Amazon Bedrock",
        "https://bedrock-mantle.${AWS_REGION}.api.aws/v1",
        "AWS_BEARER_TOKEN_BEDROCK", "openai.gpt-oss-120b", True,
        hint="Bedrock Mantle · requires AWS_REGION",
        required_env=("AWS_REGION",),
    ),
    "zai": ProviderSpec(
        "zai", "Z.AI", "https://api.z.ai/api/paas/v4",
        "ZHIPU_API_KEY", "glm-5.2",
    ),
    "github-models": ProviderSpec(
        "github-models", "GitHub Models", "https://models.github.ai/inference",
        "GITHUB_TOKEN", "openai/gpt-5.4",
    ),
    "huggingface": ProviderSpec(
        "huggingface", "Hugging Face", "https://router.huggingface.co/v1",
        "HF_TOKEN", "meta-llama/Llama-3.3-70B-Instruct",
    ),
    "ollama": ProviderSpec(
        "ollama", "Ollama (local)", "http://localhost:11434/v1",
        "OLLAMA_API_KEY", "llama3.1", keyless_default="ollama",
        hint="Local models · no key required",
    ),
    "lmstudio": ProviderSpec(
        "lmstudio", "LM Studio (local)", "http://127.0.0.1:1234/v1",
        "LMSTUDIO_API_KEY", "local-model", keyless_default="lm-studio",
        hint="Local models · no key required",
    ),
}

# The first provider screen stays intentionally small. Everything else remains
# available through the searchable "Other…" entry.
CURATED_PROVIDER_IDS = (
    "openhack",
    "anthropic",
    "openai",
    "openrouter",
    "google",
    "vercel",
    "cloudflare-workers-ai",
    "fireworks-ai",
    "groq",
    "together",
    "deepseek",
    "azure",
    "amazon-bedrock",
    "ollama",
    "moonshotai",
)


@dataclass
class ResolvedProvider:
    name: str
    base_url: str
    api_key: Optional[str]
    model: str
    supports_prompt_cache: bool
    pricing: dict
    missing_key_env: Optional[str] = None
    auth_type: str = "api"
    account_id: Optional[str] = None


def get_spec(name: str) -> Optional[ProviderSpec]:
    if name in EXCLUDED_PROVIDER_IDS:
        return None
    if name in PROVIDERS:
        return PROVIDERS[name]
    remote = provider_from_models_dev(name)
    if not remote or not remote.env:
        return None
    ids = rank_model_ids(entry["id"] for entry in remote.models)
    if not ids:
        return None
    return ProviderSpec(
        name=name,
        label=remote.name,
        base_url=remote.api,
        api_key_env=remote.env[0],
        default_model=ids[0],
        hint="Discovered through Models.dev",
    )


def is_known(name: str) -> bool:
    return name == "openhack" or get_spec(name) is not None


def list_provider_specs(*, refresh: bool = False) -> list[ProviderSpec]:
    specs = list(PROVIDERS.values())
    present = set(PROVIDERS)
    for remote in discover_compatible_providers(
        refresh=refresh, allow_network=refresh
    ):
        if (
            remote.id in EXCLUDED_PROVIDER_IDS
            or remote.id in present
            or not remote.env
        ):
            continue
        ids = rank_model_ids(model["id"] for model in remote.models)
        if not ids:
            continue
        specs.append(
            ProviderSpec(
                name=remote.id,
                label=remote.name,
                base_url=remote.api,
                api_key_env=remote.env[0],
                default_model=ids[0],
                hint="Discovered through Models.dev",
            )
        )
    return specs


def list_providers(*, refresh: bool = False) -> list[str]:
    return ["openhack", *(spec.name for spec in list_provider_specs(refresh=refresh))]


def provider_models(
    name: str,
    live_models: Optional[list[str | dict[str, str]]] = None,
) -> list[dict[str, str]]:
    if name == "openai":
        credential = get_credential("openai")
        if (
            not os.environ.get("OPENAI_API_KEY")
            and credential
            and credential.get("type") == "oauth"
        ):
            return merge_models(name, list(OPENAI_SUBSCRIPTION_MODELS))
    return merge_models(name, live_models)


def is_connected(name: str) -> bool:
    """Return whether *name* has enough persisted/runtime auth to be usable.

    A provider merely being selected in config is not a connection. Keeping
    that distinction here prevents stale selections from leaking unusable
    models into `/models`.
    """
    from openhack.config import load_user_config

    config = load_user_config()
    if name == "openhack":
        return bool(
            os.environ.get("OPENHACK_API_KEY")
            or config.get("openhack_api_key")
        )

    spec = get_spec(name)
    if spec is None:
        return False
    if any(not os.environ.get(env_name) for env_name in spec.required_env):
        return False
    if os.environ.get(spec.api_key_env):
        return True
    credential = get_credential(name) or {}
    if credential.get("type") == "api" and credential.get("key"):
        return True
    if (
        name == "openai"
        and credential.get("type") == "oauth"
        and (credential.get("access") or credential.get("refresh"))
    ):
        return True
    return bool(spec.keyless_default and config.get("provider") == name)


def connected_provider_ids(
    specs: Optional[list[ProviderSpec]] = None,
) -> set[str]:
    """Return connected provider IDs with one credential/config read.

    Picker rendering calls this for many rows, so it must not reread the
    credential store once per provider.
    """
    from openhack.config import load_user_config

    config = load_user_config()
    credentials = all_credentials()
    connected: set[str] = set()
    if os.environ.get("OPENHACK_API_KEY") or config.get("openhack_api_key"):
        connected.add("openhack")
    for spec in specs if specs is not None else list_provider_specs():
        if any(not os.environ.get(env_name) for env_name in spec.required_env):
            continue
        credential = credentials.get(spec.name) or {}
        has_oauth = (
            spec.name == "openai"
            and credential.get("type") == "oauth"
            and (credential.get("access") or credential.get("refresh"))
        )
        has_api_key = (
            credential.get("type") == "api" and credential.get("key")
        )
        if (
            os.environ.get(spec.api_key_env)
            or has_api_key
            or has_oauth
            or (spec.keyless_default and config.get("provider") == spec.name)
        ):
            connected.add(spec.name)
    return connected


def resolve(name: str, model: Optional[str] = None) -> Optional[ResolvedProvider]:
    """Resolve connection parameters from OAuth, stored key, env, and defaults."""
    if name == "openhack":
        return None
    spec = get_spec(name)
    if spec is None:
        return None

    credential = get_credential(name)
    env_key = os.environ.get(spec.api_key_env)
    if (
        name == "openai"
        and credential
        and credential.get("type") == "oauth"
    ):
        # An explicit `/connect` to ChatGPT is a provider-method choice, not a
        # fallback for machines without OPENAI_API_KEY. Developer shells often
        # carry an unrelated API key; letting that ambient variable win silently
        # moves subscription traffic onto metered platform.openai.com billing.
        return ResolvedProvider(
            name=name,
            base_url=OPENAI_CODEX_BASE_URL,
            api_key=str(credential.get("access") or ""),
            model=model or OPENAI_SUBSCRIPTION_MODELS[0],
            supports_prompt_cache=True,
            pricing={},
            auth_type="oauth",
            account_id=(
                str(credential["accountId"]) if credential.get("accountId") else None
            ),
        )

    stored_key = (
        str(credential.get("key"))
        if credential and credential.get("type") == "api" and credential.get("key")
        else None
    )
    api_key = env_key or stored_key or spec.keyless_default
    env_prefix = name.upper().replace("-", "_")
    base_url = os.path.expandvars(
        os.environ.get(f"{env_prefix}_BASE_URL", spec.base_url)
    )
    missing_required_env = next(
        (env_name for env_name in spec.required_env if not os.environ.get(env_name)),
        None,
    )
    selected_model = (
        model
        or os.environ.get(f"{env_prefix}_MODEL")
        or spec.default_model
    )
    return ResolvedProvider(
        name=name,
        base_url=base_url,
        api_key=api_key,
        model=selected_model,
        supports_prompt_cache=spec.supports_prompt_cache,
        pricing=spec.pricing,
        missing_key_env=(
            spec.api_key_env
            if not api_key
            else missing_required_env
        ),
    )
