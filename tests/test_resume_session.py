"""openhack --resume <id> reopens a saved session in the TUI: transcript
restored and, for agent sessions, a continuable agent rebuilt from the trace."""

import json
from pathlib import Path

from openhack.shells import ShellManager
from openhack.tui import OpenHackApp


def test_messages_from_trace_reconstructs_conversation():
    trace = [
        {"event_type": "user", "content": "find bugs"},
        {"event_type": "thinking", "content": "Let me look."},
        {"event_type": "tool_call", "tool_name": "run_command", "tool_input": {"command": "ls -la"}},
        {"event_type": "tool_result", "tool_output": {"exit_code": 0}},
        {"event_type": "thinking", "content": "Empty directory."},
        {"event_type": "user", "content": "anything else?"},
    ]
    msgs = OpenHackApp._messages_from_trace(trace)
    assert msgs[0].role == "user" and "find bugs" in msgs[0].content
    asst = next(m for m in msgs if m.role == "assistant")
    # assistant turn folds its text + a compact record of tool activity
    assert "Let me look." in asst.content
    assert "run_command" in asst.content and "ls -la" in asst.content
    assert "exit 0" in asst.content
    assert msgs[-1].role == "user" and "anything else" in msgs[-1].content


def test_messages_from_trace_starts_on_user_turn():
    # Leading agent chatter (no user yet) is dropped so the history is well-formed.
    trace = [
        {"event_type": "thinking", "content": "preamble"},
        {"event_type": "user", "content": "go"},
    ]
    msgs = OpenHackApp._messages_from_trace(trace)
    assert msgs and msgs[0].role == "user"


def _bare_app():
    app = OpenHackApp.__new__(OpenHackApp)
    app._invalidate = lambda: None
    app.shells = ShellManager()
    app.model = "grok-4.5"
    app.provider = "openhack"
    app.scan = None
    app.session = None
    app.agent = None
    app.is_agent_session = False
    app.mode = "landing"
    app.active_tab = "trace"
    app.viewing_target = ""
    app.last_status_line = ""
    app.last_findings = []
    app._stream_buf = ""
    app._stream_reasoning = ""
    return app


def test_resume_agent_session_is_continuable(tmp_path, monkeypatch):
    scans = tmp_path / ".openhack" / "scans"
    scans.mkdir(parents=True)
    sid = "abc12345-0000-0000-0000-000000000000"
    report = {
        "version": 2, "kind": "agent", "scan_id": sid,
        "target_dir": str(tmp_path), "status": "completed",
        "duration_seconds": 1.0,
        "cost": {"total_cost": 0.01, "total_tokens": 100},
        "findings": [],
        "trace": [
            {"timestamp": 1.0, "agent": "you", "event_type": "user", "content": "hi"},
            {"timestamp": 2.0, "agent": "openhack", "event_type": "thinking", "content": "Hey there."},
        ],
    }
    (scans / f"{sid}.json").write_text(json.dumps(report))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    app = _bare_app()
    app._resume_session(sid)

    # Rebuilt as a continuable agent session, not a read-only view.
    assert app.is_agent_session is True
    assert app.mode == "scanning"
    assert app.agent is not None and app.session is not None
    # _system_prompt set → continue_run resumes instead of cold-starting.
    assert app.agent._system_prompt
    assert app.agent.messages and app.agent.messages[0].role == "user"
    # Transcript hydrated from the saved trace.
    assert app.scan is not None and app.scan.trace_lines


def test_resume_scan_session_opens_findings_view(tmp_path, monkeypatch):
    scans = tmp_path / ".openhack" / "scans"
    scans.mkdir(parents=True)
    sid = "scan9999"
    report = {
        "version": 2, "kind": "scan", "scan_id": sid,
        "target_dir": str(tmp_path), "status": "completed",
        "duration_seconds": 5.0, "cost": {"total_cost": 0.5},
        "findings": [{"category": "sqli", "severity": "high", "title": "SQLi"}],
        "trace": [],
    }
    (scans / f"{sid}.json").write_text(json.dumps(report))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    app = _bare_app()
    app._resume_session(sid)

    assert app.mode == "viewing"
    assert app.active_tab == "findings"
    assert len(app.last_findings) == 1 and app.last_findings[0].title == "SQLi"
    assert app.is_agent_session is False
