"""CLI dispatch: `--flag` actions, legacy bare subcommands, and `openhack [path]`.

The _cmd_* handlers read their positional args straight from sys.argv, so a
--flag must sit in argv[1] exactly where the old subcommand did. These tests pin
the routing (which handler / TUI target fires) without running any handler.
"""

import contextlib
import io

import pytest

import openhack.__main__ as m


@pytest.fixture
def routed(monkeypatch):
    """Stub the TUI launcher and every command so we observe routing only."""
    calls = []
    monkeypatch.setattr(m, "_launch_tui", lambda target=None: calls.append(("tui", target)))
    stubbed = {k: (lambda name: (lambda: calls.append(("cmd", name))))(k) for k in m.COMMANDS}
    monkeypatch.setattr(m, "COMMANDS", stubbed)

    def run(*argv):
        calls.clear()
        monkeypatch.setattr("sys.argv", ["openhack", *argv])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            m.main()
        return calls[0] if calls else None, out.getvalue()

    return run


def test_no_args_launches_tui_on_cwd(routed):
    assert routed()[0] == ("tui", None)


@pytest.mark.parametrize("flag,name", [
    ("--scan", "scan"), ("--hack", "hack"), ("--plan", "plan"),
    ("--agent", "agent"), ("--sessions", "sessions"), ("--resume", "resume"),
    ("--classify", "classify"),
    ("--login", "login"), ("--setup", "setup"),
])
def test_flag_forms_route_to_command(routed, flag, name):
    assert routed(flag)[0] == ("cmd", name)


def test_flag_with_trailing_args_still_routes(routed):
    # `openhack --hack "task" /path` — handler reads argv[2]/argv[3] itself.
    assert routed("--hack", "do a thing", "/tmp")[0] == ("cmd", "hack")


def test_legacy_bare_subcommand_still_works(routed):
    assert routed("scan")[0] == ("cmd", "scan")


def test_bare_existing_dir_launches_tui_targeting_it(routed, tmp_path):
    assert routed(str(tmp_path))[0] == ("tui", str(tmp_path))


def test_bare_nondir_routes_to_tui_which_reports_error(routed):
    # Non-command, non-dir → handed to the TUI launcher (which validates).
    assert routed("/no/such/place")[0] == ("tui", "/no/such/place")


def test_unknown_option_errors(routed):
    call, out = routed("--nope")
    assert call is None
    assert "Unknown option: --nope" in out


def test_providers_cli_command_was_removed(routed):
    call, out = routed("--providers")
    assert call is None
    assert "Unknown option: --providers" in out


def test_help_and_version(routed):
    _, help_out = routed("--help")
    assert "Usage:" in help_out
    _, ver_out = routed("--version")
    assert ver_out.strip().startswith("openhack ")


def test_launch_tui_rejects_nondir_path():
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        m._launch_tui("/definitely/not/a/dir")
    assert "is not a directory" in out.getvalue()


def test_launch_tui_never_runs_first_time_setup(monkeypatch):
    calls = []
    monkeypatch.setattr("openhack.tui.main", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(
        "openhack.setup.run_first_time_setup",
        lambda: (_ for _ in ()).throw(AssertionError("startup wizard must not run")),
    )

    m._launch_tui()

    assert calls == [{"resume_session_id": None}]


def test_require_auth_passes_for_connected_byok_provider(monkeypatch):
    from openhack.config import settings
    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "openhack_api_key", None)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-openai")

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert m._require_auth() is True
    assert out.getvalue() == ""


def test_require_auth_fails_for_disconnected_byok_provider(monkeypatch):
    from openhack.config import settings
    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "openhack_api_key", None)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("openhack.provider_auth.all_credentials", lambda: {})

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert m._require_auth() is False
    assert "Error: provider 'openai' is not configured." in out.getvalue()
    assert "OPENAI_API_KEY" in out.getvalue()


def test_require_auth_fails_for_openhack_when_logged_out(monkeypatch):
    from openhack.config import settings
    monkeypatch.setattr(settings, "llm_provider", "openhack")
    monkeypatch.setattr(settings, "openhack_api_key", None)
    monkeypatch.delenv("OPENHACK_API_KEY", raising=False)
    monkeypatch.setattr("openhack.config.load_user_config", lambda: {})

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert m._require_auth() is False
    assert "Error: not logged in." in out.getvalue()


def test_require_auth_passes_for_openhack_when_logged_in(monkeypatch):
    from openhack.config import settings
    monkeypatch.setattr(settings, "llm_provider", "openhack")
    monkeypatch.setattr(settings, "openhack_api_key", "sk-openhack-test")

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert m._require_auth() is True
    assert out.getvalue() == ""


def test_cmd_scan_runs_with_byok_provider_without_openhack_key(monkeypatch, tmp_path):
    from openhack.config import settings
    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "openhack_api_key", None)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-openai")

    target_dir = tmp_path / "sample-project"
    target_dir.mkdir()
    monkeypatch.setattr("sys.argv", ["openhack", "--scan", str(target_dir)])

    scan_invoked = []

    async def _fake_run(target):
        scan_invoked.append(target)

    monkeypatch.setattr("openhack.headless_scan.run_headless_scan", _fake_run)

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        m._cmd_scan()

    assert scan_invoked == [str(target_dir)]
    assert "Error: not logged in" not in out.getvalue()

