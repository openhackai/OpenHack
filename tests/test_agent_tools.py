"""Tests for the interactive hacking toolkit: shell, security, mailbox, registry."""

from pathlib import Path

import pytest

from openhack.tools.shell import ShellTools
from openhack.tools.security_tools import SecurityTools
from openhack.tools.mailbox import MailboxTools
from openhack.tools.registry import ToolRegistry


# --------------------------------------------------------------------- shell

def test_shell_run_command_captures_stdout(tmp_path):
    sh = ShellTools(workdir=tmp_path)
    result = sh.run_command("echo hello-openhack")
    assert result["exit_code"] == 0
    assert "hello-openhack" in result["stdout"]
    assert result.get("truncated") is not True


def test_shell_run_command_reports_nonzero_exit(tmp_path):
    sh = ShellTools(workdir=tmp_path)
    result = sh.run_command("exit 7")
    assert result["exit_code"] == 7


def test_shell_run_command_stderr_and_pipes(tmp_path):
    sh = ShellTools(workdir=tmp_path)
    result = sh.run_command("echo oops 1>&2")
    assert "oops" in result["stderr"]
    piped = sh.run_command("printf 'a\\nb\\nc\\n' | grep b")
    assert piped["stdout"].strip() == "b"


def test_shell_timeout(tmp_path):
    sh = ShellTools(workdir=tmp_path)
    result = sh.run_command("sleep 5", timeout=1)
    assert result.get("timed_out") is True


def test_shell_output_truncation(tmp_path):
    sh = ShellTools(workdir=tmp_path)
    # Emit far more than the cap.
    result = sh.run_command("python3 -c \"print('x' * 200000)\"")
    assert result.get("truncated") is True
    assert len(result["stdout"]) <= ShellTools.MAX_OUTPUT_CHARS + 200


def test_shell_workdir_override(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "marker.txt").write_text("x")
    sh = ShellTools(workdir=tmp_path)
    result = sh.run_command("ls", workdir="sub")
    assert "marker.txt" in result["stdout"]


def test_shell_workdir_missing(tmp_path):
    sh = ShellTools(workdir=tmp_path)
    result = sh.run_command("ls", workdir="does-not-exist")
    assert "error" in result


def test_shell_which(tmp_path):
    sh = ShellTools(workdir=tmp_path)
    assert sh.which("python3")["installed"] is True
    assert sh.which("definitely-not-a-real-tool-xyz")["installed"] is False


def test_shell_empty_command(tmp_path):
    sh = ShellTools(workdir=tmp_path)
    assert "error" in sh.run_command("   ")


def test_shell_execute_tool_filters_unknown_args(tmp_path):
    sh = ShellTools(workdir=tmp_path)
    result = sh.execute_tool("run_command", {"command": "echo hi", "bogus": 1})
    assert result["exit_code"] == 0


# ------------------------------------------------------------------ secrets

def test_secret_scan_finds_aws_key(tmp_path):
    (tmp_path / "config.py").write_text(
        'AWS_ACCESS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"\n'
        'x = 1\n'
    )
    sec = SecurityTools(workdir=tmp_path)
    result = sec.secret_scan()
    types = {c["type"] for c in result["candidates"]}
    assert "aws_access_key_id" in types
    assert result["count"] >= 1


def test_secret_scan_finds_private_key_and_redacts(tmp_path):
    (tmp_path / "id_rsa").write_text("-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n")
    sec = SecurityTools(workdir=tmp_path)
    result = sec.secret_scan()
    assert any(c["type"] == "private_key" for c in result["candidates"])


def test_secret_scan_redacts_github_token(tmp_path):
    token = "ghp_" + "A" * 36
    (tmp_path / "app.env").write_text(f"GITHUB_TOKEN={token}\n")
    sec = SecurityTools(workdir=tmp_path)
    result = sec.secret_scan()
    hit = next(c for c in result["candidates"] if c["type"] == "github_token")
    # The full token must not appear verbatim in the preview.
    assert token not in hit["preview"]
    assert "***" in hit["preview"]


def test_secret_scan_skips_binaries_and_vendor(tmp_path):
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "leak.js").write_text(
        'const k = "AKIAIOSFODNN7EXAMPLE"'
    )
    (tmp_path / "logo.png").write_bytes(b"AKIAIOSFODNN7EXAMPLE")
    sec = SecurityTools(workdir=tmp_path)
    result = sec.secret_scan()
    assert result["count"] == 0


def test_secret_scan_clean_tree(tmp_path):
    (tmp_path / "main.py").write_text("print('hello world')\n")
    sec = SecurityTools(workdir=tmp_path)
    result = sec.secret_scan()
    assert result["count"] == 0
    assert result["engine"] == "openhack-secrets"


def test_secret_scan_missing_path(tmp_path):
    sec = SecurityTools(workdir=tmp_path)
    assert "error" in sec.secret_scan(path="nope/does/not/exist")


# ---------------------------------------------------------------------- SCA

def test_sca_scan_fallback_without_osv(tmp_path, monkeypatch):
    (tmp_path / "package-lock.json").write_text("{}")
    monkeypatch.setattr("openhack.tools.security_tools.which", lambda _: None)
    sec = SecurityTools(workdir=tmp_path)
    result = sec.sca_scan()
    assert result["engine"] == "none"
    assert "package-lock.json" in result["lockfiles"]


def test_sca_scan_uses_osv_when_present(tmp_path, monkeypatch):
    import subprocess as _sp

    sample = {
        "results": [
            {
                "source": {"path": str(tmp_path / "package-lock.json")},
                "packages": [
                    {
                        "package": {"name": "lodash", "ecosystem": "npm", "version": "4.17.11"},
                        "vulnerabilities": [
                            {
                                "id": "GHSA-xxxx",
                                "aliases": ["CVE-2019-10744"],
                                "summary": "Prototype pollution",
                                "severity": [{"score": "7.4"}],
                            }
                        ],
                    }
                ],
            }
        ]
    }

    class FakeProc:
        returncode = 1
        stdout = __import__("json").dumps(sample)
        stderr = ""

    monkeypatch.setattr("openhack.tools.security_tools.which", lambda _: "/usr/bin/osv-scanner")
    monkeypatch.setattr(_sp, "run", lambda *a, **k: FakeProc())
    sec = SecurityTools(workdir=tmp_path)
    result = sec.sca_scan()
    assert result["engine"] == "osv-scanner"
    assert result["count"] == 1
    assert result["findings"][0]["package"] == "lodash"
    assert "CVE-2019-10744" in result["findings"][0]["aliases"]


# ------------------------------------------------------------------ mailbox

def test_mailbox_unavailable_without_cli(monkeypatch):
    monkeypatch.setattr("openhack.tools.mailbox.which", lambda _: None)
    mb = MailboxTools()
    result = mb.mailbox_new()
    assert result["error"] == "mailbox_unavailable"


def test_mailbox_unconfigured_without_token(monkeypatch):
    monkeypatch.setattr("openhack.tools.mailbox.which", lambda _: "/usr/bin/inbox")
    monkeypatch.delenv("INBOX_TOKEN", raising=False)
    monkeypatch.delenv("INBOX_URL", raising=False)
    mb = MailboxTools()
    result = mb.mailbox_new()
    assert result["error"] == "mailbox_unconfigured"


def test_mailbox_wait_requires_address(monkeypatch):
    monkeypatch.setattr("openhack.tools.mailbox.which", lambda _: "/usr/bin/inbox")
    monkeypatch.setenv("INBOX_TOKEN", "tok")
    mb = MailboxTools()
    result = mb.mailbox_wait(to="")
    assert result["error"] == "missing_address"


def test_mailbox_new_parses_json(monkeypatch):
    monkeypatch.setattr("openhack.tools.mailbox.which", lambda _: "/usr/bin/inbox")
    monkeypatch.setenv("INBOX_TOKEN", "tok")

    class FakeProc:
        returncode = 0
        stdout = '{"address": "signup-abc123@inbox.openhack.com"}'
        stderr = ""

    monkeypatch.setattr(MailboxTools, "_run", lambda self, args, timeout: FakeProc())
    mb = MailboxTools()
    result = mb.mailbox_new(label="signup")
    assert result["address"] == "signup-abc123@inbox.openhack.com"


# ----------------------------------------------------------------- registry

def test_registry_excludes_agent_tools_by_default(tmp_path):
    reg = ToolRegistry(target_dir=tmp_path)
    names = {t["name"] for t in reg.get_all_tool_definitions()}
    assert "run_command" not in names
    assert "read_file" in names  # base scanning tools still present


def test_registry_includes_agent_tools_when_requested(tmp_path):
    reg = ToolRegistry(target_dir=tmp_path, include_agent_tools=True)
    names = {t["name"] for t in reg.get_all_tool_definitions()}
    for expected in ("run_command", "which", "sca_scan", "secret_scan",
                     "mailbox_new", "mailbox_wait", "read_file"):
        assert expected in names


def test_registry_dispatches_agent_tool(tmp_path):
    reg = ToolRegistry(target_dir=tmp_path, include_agent_tools=True)
    result = reg.execute_tool("run_command", {"command": "echo dispatched"})
    assert "dispatched" in result["stdout"]


def test_registry_unknown_tool(tmp_path):
    reg = ToolRegistry(target_dir=tmp_path, include_agent_tools=True)
    assert "error" in reg.execute_tool("no_such_tool", {})


def test_no_duplicate_tool_names(tmp_path):
    reg = ToolRegistry(target_dir=tmp_path, include_agent_tools=True)
    names = [t["name"] for t in reg.get_all_tool_definitions()]
    assert len(names) == len(set(names)), "tool names must be unique"


# -------------------------------------------------------- interactive agent

class _StubLLM:
    """Minimal LLM stand-in so we can construct an agent without a network."""
    model = "kimi-k2.5"


def _make_agent(tmp_path):
    from openhack.agents.interactive import InteractiveAgent
    from openhack.agents.session import Session

    session = Session(target_dir=str(tmp_path))
    tools = ToolRegistry(target_dir=tmp_path, include_agent_tools=True)
    return InteractiveAgent(llm=_StubLLM(), tools=tools, session=session)


def test_interactive_agent_system_prompt_has_operating_principles(tmp_path):
    agent = _make_agent(tmp_path)
    prompt = agent.get_system_prompt({"target_dir": str(tmp_path)})
    lower = prompt.lower()
    # Core behaviours from the product vision.
    assert "swiss-army" in lower
    assert "head start" in lower          # static tools first
    assert "plan" in lower                 # plan then act
    assert "ask" in lower                  # asks questions
    assert "verify" in lower or "confirm" in lower
    assert str(tmp_path) in prompt         # session context injected


def test_interactive_agent_exposes_full_toolkit(tmp_path):
    agent = _make_agent(tmp_path)
    names = {t["name"] for t in agent.get_tools()}
    for expected in ("run_command", "sca_scan", "secret_scan", "mailbox_new", "read_file"):
        assert expected in names
