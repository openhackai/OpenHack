import asyncio

import pytest

from openhack import updates
from openhack.tui import OpenHackApp


def test_build_update_commands_are_argument_vectors(monkeypatch):
    monkeypatch.setattr(updates.shutil, "which", lambda name: f"/usr/bin/{name}")

    assert updates.build_update_command("v1.2.3", "pipx") == [
        "/usr/bin/pipx", "install", "--force", "openhack==1.2.3"
    ]
    assert updates.build_update_command("1.2.3", "uv") == [
        "/usr/bin/uv", "tool", "install", "--force", "openhack==1.2.3"
    ]


def test_update_version_cannot_inject_shell_arguments():
    with pytest.raises(ValueError):
        updates.build_update_command("1.2.3; rm -rf nope", "pipx")


def test_dry_run_update_changes_nothing():
    result = asyncio.run(updates.install_update("0.1.3", dry_run=True))

    assert result.success is True
    assert result.method == "test"
    assert result.command == ["dry-run", "install", "openhack==0.1.3"]


def test_skipped_version_only_suppresses_same_or_older(monkeypatch, tmp_path):
    skipped = tmp_path / "skipped"
    monkeypatch.setattr(updates, "_SKIPPED_UPDATE_FILE", skipped)

    updates.save_skipped_update("0.2.0")

    assert updates.is_update_skipped("0.2.0") is True
    assert updates.is_update_skipped("0.1.9") is True
    assert updates.is_update_skipped("0.2.1") is False


def test_throttled_check_returns_cached_manifest(monkeypatch, tmp_path):
    cache = tmp_path / "manifest.json"
    cache.write_text('{"latest":{"version":"999.0.0"},"announcements":[]}')
    monkeypatch.setattr(updates, "_UPDATE_CACHE_FILE", cache)
    monkeypatch.setattr(updates, "_should_check", lambda: False)
    monkeypatch.setattr(updates, "_DISMISSED_FILE", tmp_path / "dismissed.json")

    info = asyncio.run(updates.fetch_updates())

    assert info is not None
    assert info.has_update is True
    assert info.latest is not None
    assert info.latest.version == "999.0.0"


def _app():
    app = OpenHackApp.__new__(OpenHackApp)
    app.scan_task = None
    app.last_status_line = ""
    app._invalidate = lambda: None
    return app


def test_update_test_command_opens_safe_prompt():
    app = _app()

    asyncio.run(app._cmd_update("test"))

    assert app._modal_kind.startswith("update:")
    assert app._modal_title == "Update Available"
    assert "TEST MODE" in app._modal_body
    assert "no package or files will be changed" in app._modal_body
    assert app._modal_yes_label == "update now"
    assert app._modal_no_label == "skip this version"
    assert app._modal_cancel_label == "later"


def test_test_update_reaches_completion_prompt():
    app = _app()

    asyncio.run(app._perform_update("0.1.3", test=True))

    assert app._modal_kind == "update-complete:0.1.3"
    assert app._modal_title == "Test Update Complete"
    assert "No files or packages were changed" in app._modal_body
