"""Render callbacks must survive transient OS errors during terminal redraws."""

from openhack.tui import OpenHackApp


def test_model_line_reuses_last_cwd_when_getcwd_is_interrupted(monkeypatch):
    app = OpenHackApp.__new__(OpenHackApp)
    app.model = "test-model"
    app.provider = "test-provider"
    app._last_render_cwd = "/tmp/known-cwd"

    def interrupted_getcwd():
        raise InterruptedError(4, "Interrupted system call")

    monkeypatch.setattr("openhack.tui.os.getcwd", interrupted_getcwd)

    fragments = app._model_line()
    rendered = "".join(text for _, text in fragments)

    assert "/tmp/known-cwd" in rendered
    assert "test-model" in rendered
    assert "test-provider" in rendered


def test_model_line_updates_last_known_cwd(monkeypatch):
    app = OpenHackApp.__new__(OpenHackApp)
    app.model = "test-model"
    app.provider = "test-provider"

    monkeypatch.setattr("openhack.tui.os.getcwd", lambda: "/tmp/new-cwd")

    app._model_line()

    assert app._last_render_cwd == "/tmp/new-cwd"
