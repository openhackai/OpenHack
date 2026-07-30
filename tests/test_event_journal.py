import json
from pathlib import Path

from openhack.agents.eventlog import EventJournal
from openhack.agents.session import Session, SessionStatus


def test_journal_is_append_only_hash_chained_and_redacted(tmp_path):
    path = tmp_path / "events.jsonl"
    journal = EventJournal("session-1", path)
    first = journal.append("started", {"authorization": "Bearer secret-token"})
    second = journal.append("finished", {"value": 2})

    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert [line["sequence"] for line in lines] == [1, 2]
    assert lines[0]["data"]["authorization"] == "[REDACTED]"
    assert second.previous_hash == first.event_hash
    assert lines[1]["previous_hash"] == lines[0]["event_hash"]
    assert path.stat().st_mode & 0o777 == 0o600
    assert journal.verify()["valid"] is True


def test_journal_continues_sequence_when_session_is_resumed(tmp_path):
    path = tmp_path / "events.jsonl"
    EventJournal("same-session", path).append("one")
    resumed = EventJournal("same-session", path)
    record = resumed.append("two")
    assert record.sequence == 2
    assert len(path.read_text().splitlines()) == 2


def test_journal_verification_detects_tampering(tmp_path):
    path = tmp_path / "events.jsonl"
    journal = EventJournal("session-1", path, fsync=False)
    journal.append("one", {"value": "original"})
    path.write_text(path.read_text().replace("original", "tampered"))
    verification = journal.verify()
    assert verification["valid"] is False
    assert verification["errors"][0]["error"] == "event_hash_mismatch"


def test_session_records_every_status_transition_and_cancel(tmp_path):
    session = Session(
        str(tmp_path),
        event_log_path=tmp_path / "session.events.jsonl",
    )
    session.pause()
    session.resume()
    session.cancel()

    assert session.status == SessionStatus.CANCELLED
    transitions = [
        event.data
        for event in session.events
        if event.event_type == "session_status_changed"
    ]
    assert [item["to"] for item in transitions] == [
        "running",
        "paused",
        "running",
        "cancelled",
    ]
    assert any(e.event_type == "session_cancel_requested" for e in session.events)


def test_trace_entries_carry_event_correlation(tmp_path):
    session = Session(str(tmp_path), persist_events=False)
    session.start_turn("inspect", "tester")
    entry = session.add_trace(
        "tester",
        "tool_call",
        "read it",
        tool_name="read_file",
        tool_input={"path": "a.py"},
        model_call_id="model-1",
        tool_call_id="tool-1",
    )
    assert entry.event_id
    assert entry.sequence
    assert entry.turn_id == session.current_turn_id
    assert entry.model_call_id == "model-1"
    assert entry.tool_call_id == "tool-1"
