"""The exit resume hint shows the session id only when it's actually resumable."""

from types import SimpleNamespace

from openhack.tui import _resume_hint


def test_no_app_or_session_returns_none(tmp_path):
    assert _resume_hint(None, tmp_path) is None
    assert _resume_hint(SimpleNamespace(session=None, last_session=None), tmp_path) is None


def test_session_without_saved_report_returns_none(tmp_path):
    app = SimpleNamespace(session=SimpleNamespace(id="abc123"), last_session=None)
    assert _resume_hint(app, tmp_path) is None  # report was never written


def test_hint_shows_session_id_when_report_exists(tmp_path):
    (tmp_path / "abc123.json").write_text("{}")
    app = SimpleNamespace(session=SimpleNamespace(id="abc123"), last_session=None)
    hint = _resume_hint(app, tmp_path)
    assert hint is not None
    assert "openhack --resume abc123" in hint


def test_falls_back_to_last_session(tmp_path):
    (tmp_path / "sess-9.json").write_text("{}")
    app = SimpleNamespace(session=None, last_session=SimpleNamespace(id="sess-9"))
    assert "openhack --resume sess-9" in _resume_hint(app, tmp_path)
