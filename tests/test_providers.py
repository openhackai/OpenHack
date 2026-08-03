"""Tests for multi-provider support (bring-your-own-key)."""

import pytest

from openhack import providers


def test_openhack_is_not_resolved_here():
    # The OpenHack provider is handled by the client via settings, not the registry.
    assert providers.resolve("openhack") is None


def test_unknown_provider_resolves_none():
    assert providers.resolve("does-not-exist") is None


def test_list_includes_openhack_and_byok():
    listed = providers.list_providers()
    assert listed[0] == "openhack"
    for name in ("openai", "anthropic", "openrouter", "groq", "ollama"):
        assert name in listed


def test_curated_provider_list_matches_primary_picker():
    assert providers.CURATED_PROVIDER_IDS == (
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


def test_endpoint_dependent_providers_report_required_environment(monkeypatch):
    monkeypatch.setattr(
        providers,
        "get_credential",
        lambda name: {"type": "api", "key": "saved"},
    )
    for env_name in (
        "CLOUDFLARE_ACCOUNT_ID",
        "AZURE_OPENAI_BASE_URL",
        "AWS_REGION",
    ):
        monkeypatch.delenv(env_name, raising=False)

    assert (
        providers.resolve("cloudflare-workers-ai").missing_key_env
        == "CLOUDFLARE_ACCOUNT_ID"
    )
    assert (
        providers.resolve("azure").missing_key_env
        == "AZURE_OPENAI_BASE_URL"
    )
    assert providers.resolve("amazon-bedrock").missing_key_env == "AWS_REGION"
    assert providers.resolve("vercel").missing_key_env is None

    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "account")
    monkeypatch.setenv(
        "AZURE_OPENAI_BASE_URL",
        "https://resource.openai.azure.com/openai/v1",
    )
    monkeypatch.setenv("AWS_REGION", "us-west-2")

    assert providers.resolve("cloudflare-workers-ai").base_url == (
        "https://api.cloudflare.com/client/v4/accounts/account/ai/v1"
    )
    assert providers.resolve("azure").base_url == (
        "https://resource.openai.azure.com/openai/v1"
    )
    assert providers.resolve("amazon-bedrock").base_url == (
        "https://bedrock-mantle.us-west-2.api.aws/v1"
    )


def test_opencode_plans_are_not_available():
    listed = providers.list_providers()
    for name in ("opencode", "opencode-go"):
        assert name not in listed
        assert not providers.is_known(name)
        assert providers.get_spec(name) is None
        assert providers.resolve(name) is None


def test_resolve_openai_reads_key_and_base(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    r = providers.resolve("openai")
    assert r.name == "openai"
    assert r.base_url == "https://api.openai.com/v1"
    assert r.api_key == "sk-test"
    assert r.missing_key_env is None
    assert r.model  # a default model


def test_resolve_reports_missing_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = providers.resolve("anthropic")
    assert r.missing_key_env == "ANTHROPIC_API_KEY"
    assert r.api_key is None


def test_model_override_and_base_override(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://proxy.internal/v1")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o-mini")
    r = providers.resolve("openai")
    assert r.base_url == "https://proxy.internal/v1"
    assert r.model == "gpt-4o-mini"
    # An explicit per-call model beats the env override.
    r2 = providers.resolve("openai", model="o3")
    assert r2.model == "o3"


def test_ollama_is_keyless(monkeypatch):
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    r = providers.resolve("ollama")
    assert r.api_key == "ollama"       # dummy keyless default
    assert r.missing_key_env is None
    assert r.base_url.startswith("http://localhost:11434")


def test_saved_api_key_is_used_without_environment(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(
        providers,
        "get_credential",
        lambda name: {"type": "api", "key": "saved"} if name == "openrouter" else None,
    )
    resolved = providers.resolve("openrouter")
    assert resolved.api_key == "saved"
    assert resolved.missing_key_env is None


def test_connected_state_requires_real_credentials_not_stale_selection(monkeypatch):
    monkeypatch.delenv("OPENHACK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(providers, "get_credential", lambda name: None)
    monkeypatch.setattr(
        "openhack.config.load_user_config",
        lambda: {"provider": "openai", "model": "gpt-5.6-sol"},
    )

    assert not providers.is_connected("openhack")
    assert not providers.is_connected("openai")


def test_openai_oauth_counts_as_connected(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        providers,
        "get_credential",
        lambda name: {"type": "oauth", "refresh": "refresh-token"},
    )
    monkeypatch.setattr("openhack.config.load_user_config", lambda: {})

    assert providers.is_connected("openai")


def test_openai_oauth_resolves_to_codex_responses_endpoint(monkeypatch):
    monkeypatch.setattr(
        providers,
        "get_credential",
        lambda name: {
            "type": "oauth",
            "access": "access",
            "refresh": "refresh",
            "expires": 9999999999999,
            "accountId": "account",
        },
    )
    resolved = providers.resolve("openai")
    assert resolved.auth_type == "oauth"
    assert resolved.base_url == "https://chatgpt.com/backend-api/codex"
    assert resolved.account_id == "account"
    assert resolved.model == "gpt-5.6-sol"


def test_provider_models_for_openai_oauth_match_subscription_catalog(monkeypatch):
    monkeypatch.setattr(
        providers,
        "get_credential",
        lambda name: {"type": "oauth"},
    )
    ids = [model["id"] for model in providers.provider_models("openai")]
    assert ids[:3] == ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]


# --------------------------------------------------- LLMClient integration

def test_llmclient_uses_provider_base_url(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    from openhack.agents.llm import LLMClient

    client = LLMClient(provider="openai")
    assert client.model  # provider default
    assert str(client.client.base_url).rstrip("/") == "https://api.openai.com/v1"


def test_llmclient_missing_key_raises_clear_error(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    from openhack.agents.llm import LLMClient

    with pytest.raises(ValueError) as exc:
        LLMClient(provider="groq")
    assert "GROQ_API_KEY" in str(exc.value)


def test_fetch_available_models_parses(monkeypatch):
    import openhack.agents.llm as llm

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            return b'{"object":"list","data":[{"id":"glm-5.2"},{"id":"kimi-k2.5"},{"id":"gemma-4-31b"}]}'

    monkeypatch.setattr(llm.settings, "openhack_api_key", "sk-test", raising=False)
    monkeypatch.setattr(llm.urllib.request, "urlopen", lambda *a, **k: FakeResp())
    models = llm.fetch_available_models(api_key="sk-test")
    assert models == ["glm-5.2", "kimi-k2.5", "gemma-4-31b"]


def test_fetch_available_models_none_without_key(monkeypatch):
    import openhack.agents.llm as llm
    # No explicit key and no configured key -> None (don't hit the network).
    monkeypatch.setattr(llm.settings, "openhack_api_key", None, raising=False)
    assert llm.fetch_available_models(api_key="") is None


def test_fetch_available_models_none_on_error(monkeypatch):
    import openhack.agents.llm as llm

    def boom(*a, **k):
        raise OSError("network down")

    monkeypatch.setattr(llm.urllib.request, "urlopen", boom)
    assert llm.fetch_available_models(api_key="sk-test") is None


def test_legacy_pricing_covers_non_openrouter_fallback_models():
    from openhack.agents.llm import LLMClient
    for m in (
        "glm-5.2",
        "kimi-k2.5",
        "gemma-4-31b",
        "mistral-large-2512",
    ):
        assert m in LLMClient.PRICING

    assert not any(model.startswith("gpt-5.6-") for model in LLMClient.PRICING)


def test_llmclient_unknown_provider_cost_is_zero(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    from openhack.agents.llm import LLMClient

    client = LLMClient(provider="openai", model="some-unknown-model")
    # Unknown pricing -> 0 rather than OpenHack's rates (user is billed by provider).
    assert client._calculate_cost(1_000_000, 1_000_000) == 0.0
