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
