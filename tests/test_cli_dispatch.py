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
