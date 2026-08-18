"""Tests for the interactive hacking toolkit: shell, security, mailbox, registry."""

import os
import time
from pathlib import Path

import pytest

from openhack.tools.shell import ShellTools
from openhack.tools.security_tools import SecurityTools
from openhack.tools.mailbox import MailboxTools
from openhack.tools.recon import ReconTools
from openhack.tools.oob import OOBTools
from openhack.tools.registry import ToolRegistry
from tests.conftest import shell_command


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
    producer = shell_command("print('a\\nb\\nc')")
    consumer = shell_command(
        "import sys; sys.stdout.writelines(line for line in sys.stdin if line.strip() == 'b')"
    )
    piped = sh.run_command(f"{producer} | {consumer}")
    assert piped["stdout"].strip() == "b"


def test_shell_timeout(tmp_path):
    sh = ShellTools(workdir=tmp_path)
    result = sh.run_command(shell_command("import time; time.sleep(5)"), timeout=1)
    assert result.get("timed_out") is True


def _wait(pred, timeout=4.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.03)
    return pred()


def test_shell_background_returns_id_and_bash_output(tmp_path):
    from openhack.shells import ShellManager
    mgr = ShellManager()
    sh = ShellTools(workdir=tmp_path, shells=mgr)
    started = sh.run_command(
        shell_command("print('x'); print('y')"), run_in_background=True
    )
    assert started["status"] == "running"
    sid = started["shell_id"]
    assert _wait(lambda: mgr.get(sid).status == "exited")
    out = sh.bash_output(sid)
    assert out["status"] == "exited" and out["exit_code"] == 0
    assert "x" in out["output"] and "y" in out["output"]
    # Second poll returns nothing new.
    assert sh.bash_output(sid)["output"] == ""


def test_shell_kill_shell_stops_background(tmp_path):
    from openhack.shells import ShellManager
    mgr = ShellManager()
    sh = ShellTools(workdir=tmp_path, shells=mgr)
    sid = sh.run_command(
        shell_command("import time; time.sleep(30)"), run_in_background=True
    )["shell_id"]
    assert _wait(lambda: mgr.get(sid).is_running(), timeout=1.0)
    assert sh.kill_shell(sid)["killed"] is True
    assert _wait(lambda: mgr.get(sid).proc.poll() is not None)


def test_shell_background_bad_workdir_returns_error(tmp_path):
    from openhack.shells import ShellManager
    sh = ShellTools(workdir=tmp_path, shells=ShellManager())
    r = sh.run_command("echo hi", run_in_background=True, workdir="does-not-exist")
    assert "error" in r and "does not exist" in r["error"]


def test_shell_background_tools_registered(tmp_path):
    names = {t["name"] for t in ShellTools(workdir=tmp_path).get_tool_definitions()}
    assert {"run_command", "which", "bash_output", "kill_shell"} <= names
    bg = next(t for t in ShellTools(workdir=tmp_path).get_tool_definitions()
              if t["name"] == "run_command")
    assert "run_in_background" in bg["parameters"]["properties"]


def test_shell_output_truncation(tmp_path):
    sh = ShellTools(workdir=tmp_path)
    # Emit far more than the cap.
    result = sh.run_command(shell_command("print('x' * 200000)"))
    assert result.get("truncated") is True
    assert len(result["stdout"]) <= ShellTools.MAX_OUTPUT_CHARS + 200


def test_truncated_shell_output_is_preserved_as_owner_only_artifact(tmp_path):
    from openhack.agents.session import Session

    session = Session(
        str(tmp_path),
        event_log_path=tmp_path / "events.jsonl",
    )
    sh = ShellTools(workdir=tmp_path, session=session)
    result = sh.run_command(shell_command("print('x' * 50000)"))
    artifact = result["full_output_artifact"]
    path = Path(artifact["path"])
    assert path.exists()
    assert "x" * 30_000 in path.read_text()
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    assert any(
        event.event_type == "tool_output_artifact_created"
        for event in session.events
    )


def test_shell_workdir_override(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "marker.txt").write_text("x")
    sh = ShellTools(workdir=tmp_path)
    result = sh.run_command(
        shell_command("import os; print('\\n'.join(os.listdir('.')))"),
        workdir="sub",
    )
    assert "marker.txt" in result["stdout"]


def test_shell_workdir_missing(tmp_path):
    sh = ShellTools(workdir=tmp_path)
    result = sh.run_command(shell_command("print('unused')"), workdir="does-not-exist")
    assert "error" in result


def test_shell_which(tmp_path):
    sh = ShellTools(workdir=tmp_path)
    available_tool = "cmd" if os.name == "nt" else "python3"
    assert sh.which(available_tool)["installed"] is True
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
    monkeypatch.setattr("openhack.tools.security_tools.run_killable", lambda *a, **k: FakeProc())
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


# ------------------------------------------------------------------- recon

def test_recon_reports_missing_tool(monkeypatch):
    monkeypatch.setattr("openhack.tools.recon.which", lambda _: None)
    r = ReconTools()
    result = r.subdomains("example.com")
    assert result["error"] == "tool_not_installed"
    assert result["tool"] == "subfinder"


def test_recon_subdomains_parses_output(monkeypatch):
    import openhack.tools.recon as recon

    class FakeProc:
        stdout = "a.example.com\nb.example.com\n\n"
        stderr = ""

    monkeypatch.setattr(recon, "which", lambda _: "/usr/bin/subfinder")
    monkeypatch.setattr(recon.ReconTools, "_run", lambda self, *a, **k: FakeProc())
    r = ReconTools()
    result = r.subdomains("example.com")
    assert result["count"] == 2
    assert "a.example.com" in result["subdomains"]


def test_recon_nuclei_parses_jsonl(monkeypatch):
    import openhack.tools.recon as recon

    class FakeProc:
        stdout = (
            '{"template-id":"CVE-2021-1","info":{"name":"Bad","severity":"high"},'
            '"matched-at":"https://x","type":"http"}\n'
            'not-json\n'
        )
        stderr = ""

    monkeypatch.setattr(recon, "which", lambda _: "/usr/bin/nuclei")
    monkeypatch.setattr(recon.ReconTools, "_run", lambda self, *a, **k: FakeProc())
    r = ReconTools()
    result = r.nuclei_scan("https://x")
    assert result["count"] == 1
    assert result["findings"][0]["severity"] == "high"


def test_recon_dns_missing_name():
    r = ReconTools()
    assert "error" in r.dns_lookup("")


def test_sqlmap_missing_tool(monkeypatch):
    monkeypatch.setattr("openhack.tools.recon.which", lambda _: None)
    r = ReconTools()
    out = r.sqlmap_test("http://x/item?id=1")
    assert out["error"] == "tool_not_installed"
    assert out["tool"] == "sqlmap"


def test_sqlmap_parses_injectable(monkeypatch):
    import openhack.tools.recon as recon

    class FakeProc:
        stdout = "sqlmap identified the following injection point\nParameter: id (GET)"
        stderr = ""

    monkeypatch.setattr(recon, "which", lambda _: "/usr/bin/sqlmap")
    monkeypatch.setattr(recon.ReconTools, "_run", lambda self, *a, **k: FakeProc())
    r = ReconTools()
    out = r.sqlmap_test("http://x/item?id=1")
    assert out["injectable"] is True


def test_sqlmap_missing_url():
    assert "error" in ReconTools().sqlmap_test("")


# --------------------------------------------------------------------- oob

def test_oob_register_generates_unique_marker():
    oob = OOBTools()
    a = oob.oob_register(label="ssrf")
    b = oob.oob_register(label="ssrf")
    assert a["marker"] != b["marker"]
    assert a["marker"].startswith("ssrf-")
    assert a["http_url"].endswith(a["marker"])


def test_oob_poll_requires_token(monkeypatch):
    monkeypatch.delenv("OOB_TOKEN", raising=False)
    oob = OOBTools()
    result = oob.oob_poll("abc123")
    assert result["error"] == "oob_unconfigured"


def test_oob_poll_requires_marker(monkeypatch):
    monkeypatch.setenv("OOB_TOKEN", "tok")
    oob = OOBTools()
    assert oob.oob_poll("")["error"] == "missing_marker"


def test_oob_poll_parses_hits(monkeypatch):
    import openhack.tools.oob as oobmod

    monkeypatch.setenv("OOB_TOKEN", "tok")

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            return b'{"count":1,"hits":[{"ts":1,"method":"GET","path":"/m","ip":"1.2.3.4"}]}'

    monkeypatch.setattr(oobmod.urllib.request, "urlopen", lambda *a, **k: FakeResp())
    oob = OOBTools()
    result = oob.oob_poll("m")
    assert result["fired"] is True
    assert result["interactions"] == 1


# ---------------------------------------------------------------- findings

def _session(tmp_path):
    from openhack.agents.session import Session
    return Session(target_dir=str(tmp_path))


def test_report_and_list_findings(tmp_path):
    from openhack.tools.findings import FindingsTools

    sess = _session(tmp_path)
    ft = FindingsTools(sess)
    out = ft.report_finding(title="SQLi in login", severity="high",
                            description="unparameterized query", category="sqli",
                            file_path="app/login.py", line_number=42)
    assert out["recorded"] is True
    assert out["total_findings"] == 1
    assert len(sess.findings) == 1

    listed = ft.list_findings()
    assert listed["count"] == 1
    assert listed["findings"][0]["title"] == "SQLi in login"
    assert listed["findings"][0]["severity"] == "high"


def test_report_finding_normalizes_bad_severity(tmp_path):
    from openhack.tools.findings import FindingsTools

    ft = FindingsTools(_session(tmp_path))
    ft.report_finding(title="x", severity="spicy")
    assert ft.list_findings()["findings"][0]["severity"] == "medium"


def test_report_finding_requires_title(tmp_path):
    from openhack.tools.findings import FindingsTools

    assert "error" in FindingsTools(_session(tmp_path)).report_finding(title="")


def test_registry_registers_findings_tools_with_session(tmp_path):
    sess = _session(tmp_path)
    reg = ToolRegistry(target_dir=tmp_path, include_agent_tools=True, session=sess)
    names = {t["name"] for t in reg.get_all_tool_definitions()}
    assert "report_finding" in names
    assert "list_findings" in names
    # A finding recorded via the tool lands in the session.
    reg.execute_tool("report_finding", {"title": "t", "severity": "low"})
    assert len(sess.findings) == 1


def test_registry_without_session_has_no_findings_tools(tmp_path):
    reg = ToolRegistry(target_dir=tmp_path, include_agent_tools=True)
    names = {t["name"] for t in reg.get_all_tool_definitions()}
    assert "report_finding" not in names


# ----------------------------------------------------------------- browser

def test_browser_degrades_without_playwright(monkeypatch):
    import asyncio
    import builtins
    from openhack.tools.browser import BrowserTools

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("playwright"):
            raise ImportError("no playwright")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    bt = BrowserTools()
    result = asyncio.run(bt.browser_fetch("https://example.com"))
    assert result["error"] == "browser_unavailable"


def test_browser_missing_url():
    import asyncio
    from openhack.tools.browser import BrowserTools

    result = asyncio.run(BrowserTools().browser_fetch(""))
    assert result["error"] == "missing_url"


def test_registry_browser_is_async(tmp_path):
    reg = ToolRegistry(target_dir=tmp_path, include_agent_tools=True)
    assert reg.is_async_tool("browser_fetch") is True
    assert reg.is_async_tool("run_command") is False
    names = {t["name"] for t in reg.get_all_tool_definitions()}
    assert "browser_fetch" in names


def test_registry_async_dispatch(tmp_path):
    import asyncio
    reg = ToolRegistry(target_dir=tmp_path, include_agent_tools=True)
    # Calling an async tool synchronously must not silently misbehave.
    sync = reg.execute_tool("browser_fetch", {"url": "https://x"})
    assert "error" in sync
    # Async path returns a real dict (degraded if no playwright, but a dict).
    out = asyncio.run(reg.execute_tool_async("browser_fetch", {"url": ""}))
    assert isinstance(out, dict)


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
                     "mailbox_new", "mailbox_wait", "read_file",
                     "subdomains", "http_probe", "port_scan", "nuclei_scan",
                     "sqlmap_test", "oob_register", "oob_poll"):
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
    assert "exactly what's asked" in lower  # scope discipline (do only the ask)
    assert "deterministic tools first" in lower  # static tools first (broad tasks)
    assert "plan" in lower                 # plan then act
    assert "ask" in lower                  # asks questions
    assert "verify" in lower or "confirm" in lower
    assert str(tmp_path) in prompt         # session context injected


def test_interactive_agent_exposes_full_toolkit(tmp_path):
    agent = _make_agent(tmp_path)
    names = {t["name"] for t in agent.get_tools()}
    for expected in ("run_command", "sca_scan", "secret_scan", "mailbox_new", "read_file"):
        assert expected in names


def _make_plan_agent(tmp_path):
    from openhack.agents.interactive import PlanAgent
    from openhack.agents.session import Session

    session = Session(target_dir=str(tmp_path))
    tools = ToolRegistry(target_dir=tmp_path, include_agent_tools=True)
    return PlanAgent(llm=_StubLLM(), tools=tools, session=session)


def test_plan_agent_is_read_only(tmp_path):
    """Plan mode must not expose attack/mutation tools."""
    agent = _make_plan_agent(tmp_path)
    names = {t["name"] for t in agent.get_tools()}
    # Passive intel is allowed...
    assert "sca_scan" in names
    assert "secret_scan" in names
    assert "read_file" in names
    # ...but nothing that executes attacks or mutates state.
    assert "run_command" not in names
    assert "mailbox_new" not in names
    assert "mailbox_wait" not in names


def test_plan_agent_prompt_is_planning(tmp_path):
    agent = _make_plan_agent(tmp_path)
    prompt = agent.get_system_prompt({"target_dir": str(tmp_path)}).lower()
    assert "plan mode" in prompt
    assert "read-only" in prompt
    assert "approve" in prompt


# ------------------------------------------------------- runner robustness

class _FakeSession:
    total_cost = 0.0
    total_tokens = 0


def test_runner_falls_back_to_last_text_on_empty_response(capsys):
    """A tool-only final turn must not leave the operator with a blank screen."""
    from openhack import interactive_runner as ir

    ir._print_result({"response": ""}, _FakeSession(), fallback="the recovered plan text")
    out = capsys.readouterr().out
    assert "the recovered plan text" in out


def test_runner_prints_response_over_fallback(capsys):
    from openhack import interactive_runner as ir

    ir._print_result({"response": "real answer"}, _FakeSession(), fallback="stale")
    out = capsys.readouterr().out
    assert "real answer" in out
    assert "stale" not in out


def test_runner_dedupes_already_streamed_answer(capsys):
    """A final answer already shown live must not be printed a second time."""
    from openhack import interactive_runner as ir

    ir._print_result({"response": "the plan body"}, _FakeSession(), fallback="the plan body")
    out = capsys.readouterr().out
    # Appears zero times in the result block (it was already streamed).
    assert out.count("the plan body") == 0


def test_runner_trace_printer_stashes_last_text():
    from openhack import interactive_runner as ir
    from openhack.agents.session import TraceEntry

    state: dict = {}
    printer = ir._make_trace_printer(state)
    printer(TraceEntry(timestamp=0.0, agent="a", event_type="thinking", content="hello plan"))
    assert state["last_text"] == "hello plan"
