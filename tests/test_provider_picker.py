import asyncio

from prompt_toolkit.buffer import Buffer

from openhack import providers
from openhack.tui import OpenHackApp, _SLASH_COMMANDS


def test_command_surface_uses_connect_and_models_without_provider():
    commands = {command for command, _ in _SLASH_COMMANDS}
    assert "/connect" in commands
    assert "/models" in commands
    assert "/fast" in commands
    assert "/tips" in commands
    assert "/provider" not in commands
    assert "/providers" not in commands
    assert "/model" not in commands


def test_model_picker_groups_all_connected_provider_catalogs(monkeypatch):
    app = _searchable_app()
    app.provider = "openai"
    app.model = "gpt-a"
    specs = [
        providers.ProviderSpec(
            "openai", "OpenAI", "https://api.openai.com/v1",
            "OPENAI_API_KEY", "gpt-a",
        ),
        providers.ProviderSpec(
            "anthropic", "Anthropic", "https://api.anthropic.com/v1",
            "ANTHROPIC_API_KEY", "claude-a",
        ),
    ]
    monkeypatch.setattr(providers, "list_provider_specs", lambda: specs)
    monkeypatch.setattr(
        providers,
        "connected_provider_ids",
        lambda specs: {"openhack", "openai"},
    )
    monkeypatch.setattr(
        "openhack.provider_auth.all_credentials",
        lambda: {"openai": {"type": "api", "key": "saved"}},
    )
    monkeypatch.setattr(
        "openhack.tui.load_user_config",
        lambda: {
            "recent_models": [
                {"provider": "openhack", "model": "grok-4.5"},
            ]
        },
    )
    monkeypatch.setattr(
        providers,
        "provider_models",
        lambda name: {
            "openhack": [
                {"id": "grok-4.5", "label": "Grok 4.5", "desc": ""},
                {"id": "gpt-5.6-luna", "label": "GPT-5.6 Luna", "desc": ""},
            ],
            "openai": [
                {"id": "gpt-a", "label": "GPT A", "desc": ""},
                {"id": "gpt-b", "label": "GPT B", "desc": ""},
            ],
        }[name],
    )

    app._open_model_picker()

    assert app.mode == "models"
    assert app.previous_mode == "landing"
    assert [(model["section"], model["id"]) for model in app.model_index] == [
        ("Recent", "gpt-a"),
        ("Recent", "grok-4.5"),
        ("OpenHack", "grok-4.5"),
        ("OpenAI models served by OpenHack", "gpt-5.6-luna"),
        ("OpenAI", "gpt-a"),
        ("OpenAI", "gpt-b"),
    ]


def _searchable_app():
    app = OpenHackApp.__new__(OpenHackApp)
    app.provider = "openhack"
    app.model = "grok-4.5"
    app.mode = "landing"
    app.previous_mode = None
    app.last_status_line = ""
    app._provider_refresh_started = True
    app._provider_query = ""
    app._provider_show_all = False
    app._provider_specs = []
    app._provider_all = []
    app.provider_index = []
    app.provider_selected = 0
    app._model_query = ""
    app._model_all = []
    app.model_index = []
    app.model_selected = 0
    app.input_buffer = Buffer(multiline=False)
    app._invalidate = lambda: None
    return app


def test_provider_search_prioritizes_title_and_supports_fuzzy_matches():
    app = _searchable_app()
    app._provider_all = [
        {
            "id": "openai",
            "label": "OpenAI",
            "hint": "ChatGPT",
            "connected": False,
            "category": "Popular",
        },
        {
            "id": "openrouter",
            "label": "OpenRouter",
            "hint": "",
            "connected": False,
            "category": "Popular",
        },
        {
            "id": "together",
            "label": "Together AI",
            "hint": "",
            "connected": False,
            "category": "Providers",
        },
    ]
    app.provider_index = list(app._provider_all)
    app._provider_query = "opnai"

    app._filter_provider_index()

    assert [entry["id"] for entry in app.provider_index] == ["openai"]


def test_open_provider_picker_never_resolves_every_provider(monkeypatch):
    app = _searchable_app()
    specs = [
        providers.ProviderSpec(
            "openai", "OpenAI", "https://api.openai.com/v1",
            "OPENAI_API_KEY", "gpt-5.6-sol",
        )
    ]
    monkeypatch.setattr(providers, "list_provider_specs", lambda: specs)
    monkeypatch.setattr(
        providers,
        "resolve",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("picker must not resolve rows")
        ),
    )
    monkeypatch.setattr("openhack.provider_auth.all_credentials", lambda: {})

    app._open_provider_picker()

    assert app.mode == "providers"
    assert [entry["id"] for entry in app.provider_index] == [
        "openhack",
        "openai",
        "__other__",
    ]


def test_other_entry_reveals_long_tail_and_escape_returns_to_curated(monkeypatch):
    app = _searchable_app()
    app._provider_specs = [
        providers.ProviderSpec(
            "openai", "OpenAI", "https://api.openai.com/v1",
            "OPENAI_API_KEY", "gpt-5.6-sol",
        ),
        providers.ProviderSpec(
            "xai", "xAI", "https://api.x.ai/v1",
            "XAI_API_KEY", "grok-4.5",
        ),
    ]
    monkeypatch.setattr("openhack.provider_auth.all_credentials", lambda: {})
    app._provider_all = app._provider_entries(app._provider_specs)
    app.provider_index = list(app._provider_all)
    app.provider_selected = next(
        index
        for index, entry in enumerate(app.provider_index)
        if entry["id"] == "__other__"
    )

    app._select_provider_from_picker()

    assert app._provider_show_all
    assert [entry["id"] for entry in app.provider_index] == ["xai"]

    app._close_provider_picker()

    assert not app._provider_show_all
    assert [entry["id"] for entry in app.provider_index] == [
        "openhack",
        "openai",
        "__other__",
    ]


def test_connect_without_provider_opens_searchable_connect_picker():
    app = _searchable_app()
    opened = []
    app._open_provider_picker = lambda: opened.append(True)

    asyncio.run(app._cmd_connect(""))

    assert opened == [True]


def test_fast_mode_persists_for_openhack_inference(monkeypatch):
    app = _searchable_app()
    saved = []
    monkeypatch.setattr("openhack.tui.settings.fast_mode", False)
    monkeypatch.setattr(
        "openhack.tui.save_user_config", lambda values: saved.append(values)
    )
    monkeypatch.setattr("openhack.tui.reload_settings", lambda: None)

    app._cmd_fast("on")

    assert saved == [{"fast_mode": True}]
    assert "fast mode on" in app.last_status_line


def test_fast_mode_rejects_direct_provider(monkeypatch):
    app = _searchable_app()
    app.provider = "openai"
    saved = []
    monkeypatch.setattr("openhack.tui.settings.fast_mode", False)
    monkeypatch.setattr(
        "openhack.tui.save_user_config", lambda values: saved.append(values)
    )

    app._cmd_fast("on")

    assert saved == []
    assert "uses OpenHack inference" in app.last_status_line


def test_tips_toggle_persists_and_refreshes_rotation(monkeypatch):
    app = _searchable_app()
    saved = []
    refreshed = []
    monkeypatch.setattr("openhack.tui.settings.tips_enabled", False)
    monkeypatch.setattr(
        "openhack.tui.save_user_config", lambda values: saved.append(values)
    )
    monkeypatch.setattr("openhack.tui.reload_settings", lambda: None)
    app._advance_tip = lambda: refreshed.append(True)

    app._cmd_tips("on")

    assert saved == [{"tips_enabled": True}]
    assert refreshed == [True]
    assert app.last_status_line == "tips on · rotating every 10 seconds"


def test_tip_rotation_includes_context_and_avoids_immediate_repeat(monkeypatch):
    app = _searchable_app()
    app.last_findings = [object()]
    app.session = None
    app.shells = type("Shells", (), {"list": lambda self: []})()
    app._tip_index = -1
    app._tip_text = ""
    monkeypatch.setattr(providers, "is_connected", lambda name: True)

    candidates = app._tip_candidates()
    assert candidates[0].startswith("Review findings")

    app._advance_tip()
    first = app._tip_text
    app._advance_tip()
    assert app._tip_text != first


def test_model_search_filters_large_provider_catalog():
    app = _searchable_app()
    app._model_all = [
        {
            "id": "gpt-5.6-sol", "label": "GPT-5.6 Sol", "desc": "frontier",
            "provider": "openai", "provider_label": "OpenAI",
            "section": "OpenAI", "recent": False,
        },
        {
            "id": "claude-sonnet-5", "label": "Claude Sonnet 5", "desc": "",
            "provider": "anthropic", "provider_label": "Anthropic",
            "section": "Anthropic", "recent": False,
        },
        {
            "id": "gemini-3.1-pro", "label": "Gemini 3.1 Pro", "desc": "",
            "provider": "google", "provider_label": "Google AI Studio",
            "section": "Google AI Studio", "recent": False,
        },
    ]
    app.model_index = list(app._model_all)
    app._model_query = "sonet"

    app._filter_model_index()

    assert [model["id"] for model in app.model_index] == ["claude-sonnet-5"]


def test_api_key_entry_activates_provider_default_model(monkeypatch):
    app = _searchable_app()
    app._provider_auth_id = "openrouter"
    app._provider_auth_label = "OpenRouter"
    app.mode = "provider_key"
    stored = []
    saved = []
    monkeypatch.setattr(
        "openhack.provider_auth.set_api_key",
        lambda provider_id, secret: stored.append((provider_id, secret)),
    )
    monkeypatch.setattr(providers, "is_connected", lambda name: name == "openrouter")
    monkeypatch.setattr(
        providers,
        "resolve",
        lambda name: providers.ResolvedProvider(
            name=name,
            base_url="https://openrouter.ai/api/v1",
            api_key="secret-value",
            model="anthropic/claude-sonnet-5",
            supports_prompt_cache=True,
            pricing={},
        ),
    )
    monkeypatch.setattr(
        "openhack.tui.save_user_config", lambda values: saved.append(values)
    )
    monkeypatch.setattr("openhack.tui.reload_settings", lambda: None)
    app._save_provider_api_key("secret-value")

    assert stored == [("openrouter", "secret-value")]
    assert (app.provider, app.model) == (
        "openrouter",
        "anthropic/claude-sonnet-5",
    )
    assert saved == [{
        "provider": "openrouter",
        "model": "anthropic/claude-sonnet-5",
    }]
    assert app.last_status_line == (
        "connected: OpenRouter · anthropic/claude-sonnet-5"
    )


def test_model_picker_hides_openhack_without_openhack_credentials(monkeypatch):
    app = _searchable_app()
    app.provider = "openai"
    app.model = "gpt-5.6-sol"
    spec = providers.get_spec("openai")
    monkeypatch.setattr(providers, "list_provider_specs", lambda: [spec])
    monkeypatch.setattr(
        providers, "connected_provider_ids", lambda specs: {"openai"}
    )
    monkeypatch.setattr(
        providers,
        "provider_models",
        lambda name: [{"id": "gpt-5.6-sol", "label": "GPT-5.6 Sol", "desc": ""}],
    )
    monkeypatch.setattr("openhack.tui.load_user_config", lambda: {})

    entries = app._connected_model_entries()

    assert entries
    assert {entry["provider"] for entry in entries} == {"openai"}
    assert not any(entry["section"].startswith("OpenAI models served") for entry in entries)


def test_selecting_model_switches_provider_and_model(monkeypatch):
    app = _searchable_app()
    app.mode = "models"
    app.previous_mode = "landing"
    app.model_index = [{
        "id": "claude-sonnet-5",
        "label": "Claude Sonnet 5",
        "provider": "anthropic",
        "provider_label": "Anthropic",
        "section": "Anthropic",
        "recent": False,
    }]
    saved = []
    monkeypatch.setattr(
        "openhack.tui.save_user_config",
        lambda values: saved.append(values),
    )
    monkeypatch.setattr("openhack.tui.load_user_config", lambda: {})

    app._select_model_from_picker()

    assert app.provider == "anthropic"
    assert app.model == "claude-sonnet-5"
    assert saved == [{
        "provider": "anthropic",
        "model": "claude-sonnet-5",
        "recent_models": [{
            "provider": "anthropic",
            "model": "claude-sonnet-5",
        }],
    }]
