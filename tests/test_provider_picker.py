import asyncio

from prompt_toolkit.buffer import Buffer

from openhack import providers
from openhack.tui import OpenHackApp, _SLASH_COMMANDS


def test_command_surface_uses_connect_and_models_without_provider():
    commands = {command for command, _ in _SLASH_COMMANDS}
    assert "/connect" in commands
    assert "/models" in commands
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


def test_api_key_entry_connects_without_switching_active_model(monkeypatch):
    app = _searchable_app()
    app._provider_auth_id = "openrouter"
    app._provider_auth_label = "OpenRouter"
    app.mode = "provider_key"
    stored = []
    original = (app.provider, app.model)
    monkeypatch.setattr(
        "openhack.provider_auth.set_api_key",
        lambda provider_id, secret: stored.append((provider_id, secret)),
    )
    app._save_provider_api_key("secret-value")

    assert stored == [("openrouter", "secret-value")]
    assert (app.provider, app.model) == original
    assert app.last_status_line == "connected: OpenRouter"


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
