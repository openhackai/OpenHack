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


def test_llmclient_unknown_provider_cost_is_zero(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    from openhack.agents.llm import LLMClient

    client = LLMClient(provider="openai", model="some-unknown-model")
    # Unknown pricing -> 0 rather than OpenHack's rates (user is billed by provider).
    assert client._calculate_cost(1_000_000, 1_000_000) == 0.0
