import asyncio

from prompt_toolkit.buffer import Buffer

from openhack import providers
from openhack.tui import OpenHackApp


def test_provider_command_without_argument_opens_picker():
    app = OpenHackApp.__new__(OpenHackApp)
    called = []
    app._open_provider_picker = lambda: called.append(True)

    app._cmd_provider("")

    assert called == [True]


def test_model_picker_uses_active_provider_catalog(monkeypatch):
    app = OpenHackApp.__new__(OpenHackApp)
    app.provider = "opencode-go"
    app.model = "grok-4.5"
    app.mode = "landing"
    app.previous_mode = None
    app.last_status_line = "old"
    app._invalidate = lambda: None
    monkeypatch.setattr(
        providers,
        "provider_models",
        lambda name: [
            {"id": "grok-4.5", "label": "Grok 4.5", "desc": ""},
            {"id": "kimi-k3", "label": "Kimi K3", "desc": ""},
        ],
    )

    app._open_model_picker()

    assert app.mode == "models"
    assert app.previous_mode == "landing"
    assert [model["id"] for model in app.model_index] == ["grok-4.5", "kimi-k3"]


def _searchable_app():
    app = OpenHackApp.__new__(OpenHackApp)
    app.provider = "openhack"
    app.model = "grok-4.5"
    app.mode = "landing"
    app.previous_mode = None
    app.last_status_line = ""
    app._provider_refresh_started = True
    app._provider_query = ""
    app._provider_action = "switch"
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
    ]


def test_connect_without_provider_opens_searchable_connect_picker():
    app = _searchable_app()
    opened = []
    app._open_provider_picker = lambda action="switch": opened.append(action)

    asyncio.run(app._cmd_connect(""))

    assert opened == ["connect"]


def test_model_search_filters_large_provider_catalog():
    app = _searchable_app()
    app._model_all = [
        {"id": "gpt-5.6-sol", "label": "GPT-5.6 Sol", "desc": "frontier"},
        {"id": "claude-sonnet-5", "label": "Claude Sonnet 5", "desc": ""},
        {"id": "gemini-3.1-pro", "label": "Gemini 3.1 Pro", "desc": ""},
    ]
    app.model_index = list(app._model_all)
    app._model_query = "sonet"

    app._filter_model_index()

    assert [model["id"] for model in app.model_index] == ["claude-sonnet-5"]


def test_api_key_entry_stores_secret_and_opens_model_picker(monkeypatch):
    app = _searchable_app()
    app._provider_auth_id = "openrouter"
    app._provider_auth_label = "OpenRouter"
    app.mode = "provider_key"
    stored = []
    opened = []
    monkeypatch.setattr(
        "openhack.provider_auth.set_api_key",
        lambda provider_id, secret: stored.append((provider_id, secret)),
    )
    app._activate_connected_provider = lambda provider_id: setattr(
        app, "provider", provider_id
    )
    app._open_model_picker = lambda: opened.append(True)

    app._save_provider_api_key("secret-value")

    assert stored == [("openrouter", "secret-value")]
    assert opened == [True]
