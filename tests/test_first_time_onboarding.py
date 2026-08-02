from types import SimpleNamespace

import pytest

from openhack import setup


@pytest.mark.asyncio
async def test_openhack_onboarding_verifies_and_defaults_to_glm(monkeypatch):
    choices = iter([0, 0, 0])  # OpenHack, browser login, GLM 5.2
    saved = []

    async def select(*args, **kwargs):
        return next(choices)

    async def login(_url):
        return SimpleNamespace(
            token="token",
            org_id=None,
            org_slug=None,
            org_name=None,
            user_email="user@example.com",
            user_first_name=None,
            user_last_name=None,
        )

    monkeypatch.setattr(setup, "_select_menu_async", select)
    monkeypatch.setattr(setup, "device_login", login)
    monkeypatch.setattr(setup, "save_user_config", lambda value: saved.append(value))
    monkeypatch.setattr(setup, "reload_settings", lambda: None)
    monkeypatch.setattr(
        "openhack.agents.llm.fetch_available_models",
        lambda **kwargs: ["grok-4.5", "glm-5.2", "kimi-k2.5"],
    )

    assert await setup._run_first_time_onboarding() is True
    assert saved[-1]["provider"] == "openhack"
    assert saved[-1]["model"] == "glm-5.2"
    assert saved[-1]["onboarding_version"] == 1


@pytest.mark.asyncio
async def test_openhack_onboarding_does_not_save_unverified_key(monkeypatch):
    choices = iter([0, 1])  # OpenHack, API key
    saved = []

    async def select(*args, **kwargs):
        return next(choices)

    async def api_key(*args, **kwargs):
        return "bad-token"

    monkeypatch.setattr(setup, "_select_menu_async", select)
    monkeypatch.setattr(setup, "_prompt_api_key", api_key)
    monkeypatch.setattr(setup, "save_user_config", lambda value: saved.append(value))
    monkeypatch.setattr(
        "openhack.agents.llm.fetch_available_models",
        lambda **kwargs: None,
    )

    assert await setup._run_first_time_onboarding() is False
    assert saved == []


@pytest.mark.asyncio
async def test_external_provider_onboarding_reuses_connect_flow(monkeypatch):
    saved = []

    async def select(*args, **kwargs):
        return 1  # OpenAI

    async def connect(provider_id, **kwargs):
        assert provider_id == "openai"
        assert kwargs["allow_back"] is True
        return True

    monkeypatch.setattr(setup, "_select_menu_async", select)
    monkeypatch.setattr(setup, "run_provider_connect", connect)
    monkeypatch.setattr(setup, "save_user_config", lambda value: saved.append(value))

    assert await setup._run_first_time_onboarding() is True
    assert saved == [{"onboarding_version": 1}]


@pytest.mark.asyncio
async def test_back_from_provider_credentials_returns_to_provider_menu(monkeypatch):
    choices = iter([3, -1])  # Google, then cancel from provider menu
    connect_calls = []

    async def select(*args, **kwargs):
        return next(choices)

    async def connect(provider_id, **kwargs):
        connect_calls.append((provider_id, kwargs))
        return False  # Esc/blank from the credential prompt

    monkeypatch.setattr(setup, "_select_menu_async", select)
    monkeypatch.setattr(setup, "run_provider_connect", connect)

    assert await setup._run_first_time_onboarding() is False
    assert connect_calls == [("google", {"allow_back": True})]


@pytest.mark.asyncio
async def test_back_from_openhack_auth_returns_to_provider_menu(monkeypatch):
    choices = iter([0, -1, -1])  # OpenHack, back, then cancel onboarding
    titles = []

    async def select(title, *args, **kwargs):
        titles.append((title, kwargs.get("cancel_label")))
        return next(choices)

    monkeypatch.setattr(setup, "_select_menu_async", select)

    assert await setup._run_first_time_onboarding() is False
    assert titles == [
        ("Connect a provider", None),
        ("Connect OpenHack", "go back"),
        ("Connect a provider", None),
    ]
