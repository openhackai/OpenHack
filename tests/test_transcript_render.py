"""Transcript rendering: compact tool rows, full-width user band, no spills."""

import time

from openhack.agents.session import TraceEntry
from openhack.tui import OpenHackApp, ScanState


def _state():
    return ScanState(target="/tmp")


def _add(s, agent, etype, content=None, **kw):
    s.update_from_trace(
        TraceEntry(timestamp=time.time(), agent=agent, event_type=etype, content=content, **kw)
    )


def _text(s):
    return ["".join(t for _, t in frags) for _, frags in s.trace_lines]


def test_tool_result_folds_onto_the_tool_call_row():
    s = _state()
    _add(s, "openhack", "tool_call", "", tool_name="run_command",
         tool_input={"command": "ls -la"})
    _add(s, "openhack", "tool_result", "", tool_name="run_command",
         tool_output={"exit_code": 0})
    lines = _text(s)
    assert len(lines) == 1, "tool call + result must share one row"
    assert "run_command" in lines[0] and "ls -la" in lines[0] and "exit 0" in lines[0]


def test_finish_task_is_never_rendered_as_an_operator_tool():
    s = _state()
    _add(
        s,
        "openhack",
        "tool_call",
        "",
        tool_name="finish_task",
        tool_input={"summary": "Done.", "reason": "completed"},
    )
    _add(
        s,
        "openhack",
        "tool_result",
        "",
        tool_name="finish_task",
        tool_output={"finished": True, "summary": "Done."},
    )

    assert _text(s) == []


def test_multiline_command_is_collapsed_to_one_row():
    # A heredoc / python -c block used to spill raw source into the transcript.
    s = _state()
    _add(s, "openhack", "tool_call", "", tool_name="run_command",
         tool_input={"command": 'python3 -c "\nimport sys\ntext = sys.stdin.read()\n"'})
    lines = _text(s)
    assert len(lines) == 1
    assert "\n" not in lines[0]
    assert "import sys" in lines[0]  # collapsed, not dropped


def test_long_command_is_truncated():
    s = _state()
    _add(s, "openhack", "tool_call", "", tool_name="run_command",
         tool_input={"command": "curl " + "x" * 500})
    line = _text(s)[0]
    assert "…" in line and len(line) < 200


def test_tool_error_is_styled_distinctly():
    s = _state()
    _add(s, "openhack", "tool_call", "", tool_name="browser_fetch",
         tool_input={"url": "https://x/"})
    _add(s, "openhack", "tool_result", "", tool_name="browser_fetch",
         tool_output={"error": "browser_unavailable"})
    styles = [style for style, _ in s.trace_lines[0][1]]
    assert "class:trace.fail" in styles


def test_scan_pipeline_rows_are_unchanged():
    # Only the interactive agent folds results; the scan pipeline keeps its
    # per-agent attribution rows.
    s = _state()
    _add(s, "hunter:auth", "tool_call", "", tool_name="grep", tool_input={"pattern": "eval"})
    assert "hunter:auth" in _text(s)[0]


def _stream_app():
    app = OpenHackApp.__new__(OpenHackApp)
    app._spin_idx = 0
    app._stream_buf = ""
    app._stream_reasoning = ""
    app._stream_tool_bytes = 0
    app._interrupting = False
    app._shell_active = False
    app.is_agent_session = True
    return app


def test_live_spinner_shows_in_every_streaming_state():
    # The transcript tail must stay visibly alive throughout a turn. It used to
    # drop the spinner the moment content began streaming (verb → "responding"),
    # so the chat log looked stalled while only the bottom bar kept moving.
    app = _stream_app()
    states = {
        "waiting": lambda: None,
        "thinking": lambda: setattr(app, "_stream_reasoning", "considering the route"),
        "responding": lambda: (setattr(app, "_stream_reasoning", ""),
                               setattr(app, "_stream_buf", "here is the chain")),
        # Streaming a large tool argument (writing a file) is the longest
        # silent stretch of a turn — it needs a spinner most of all.
        "writing tool args": lambda: (setattr(app, "_stream_buf", ""),
                                      setattr(app, "_stream_tool_bytes", 4096)),
    }
    for label, setup in states.items():
        setup()
        styles = [s for s, _ in app._stream_line()]
        assert "class:spinner" in styles, f"no live spinner while {label}"


def test_streaming_tail_still_shows_the_answer_and_caret():
    app = _stream_app()
    app._stream_buf = "here is the exploit chain"
    frags = app._stream_line()
    rendered = "".join(t for _, t in frags)
    assert "here is the exploit chain" in rendered
    assert rendered.endswith("▌")           # live cursor preserved


def test_spinner_frame_advances_with_spin_idx():
    app = _stream_app()
    app._stream_buf = "streaming"
    first = "".join(t for _, t in app._stream_line())
    app._spin_idx += 1
    assert "".join(t for _, t in app._stream_line()) != first


def test_web_search_row_shows_the_query_and_result_count():
    # A tool row that doesn't say WHAT it searched for is useless to the
    # operator watching the run.
    s = _state()
    _add(s, "openhack", "tool_call", "", tool_name="web_search",
         tool_input={"query": "CVE-2026-63030 wordpress batch"})
    _add(s, "openhack", "tool_result", "", tool_name="web_search",
         tool_output={"engine": "openhack", "count": 4})
    line = _text(s)[0]
    assert "CVE-2026-63030 wordpress batch" in line
    assert "4 results" in line


def test_web_fetch_row_shows_url_and_outcome():
    s = _state()
    _add(s, "openhack", "tool_call", "", tool_name="web_fetch",
         tool_input={"url": "https://hadrian.io/blog/wp2shell"})
    _add(s, "openhack", "tool_result", "", tool_name="web_fetch",
         tool_output={"status": 200, "text": "x" * 5998})
    line = _text(s)[0]
    assert "https://hadrian.io/blog/wp2shell" in line
    assert "200" in line and "5,998 chars" in line


def test_blocked_fetch_row_says_blocked():
    s = _state()
    _add(s, "openhack", "tool_call", "", tool_name="web_fetch",
         tool_input={"url": "https://slcyber.io/x"})
    _add(s, "openhack", "tool_result", "", tool_name="web_fetch",
         tool_output={"status": 403, "text": "", "blocked": True})
    assert "blocked (403)" in _text(s)[0]


def test_textual_status_results_render(monkeypatch):
    # dispatch_specialist / backgrounded run_command report a word, not a count.
    s = _state()
    _add(s, "openhack", "tool_call", "", tool_name="dispatch_specialist",
         tool_input={"vuln_class": "xss", "target": "https://x/"})
    _add(s, "openhack", "tool_result", "", tool_name="dispatch_specialist",
         tool_output={"status": "exploited"})
    line = _text(s)[0]
    assert "xss" in line and "exploited" in line


def test_agent_ledger_records_the_search_query():
    # The agent's own anti-repetition ledger must remember WHAT it searched,
    # otherwise it can't tell two searches apart.
    from openhack.agents.base import BaseAgent
    hint = BaseAgent._arg_hint({"query": "wp2shell batch api"})
    assert hint == "wp2shell batch api"


def _tool_row(cmd_len=200):
    return [
        ("class:trace.time", ""),
        ("class:trace.tool.dot", "  ⏺ "),
        ("class:trace.tool.name", "run_command"),
        ("class:trace.dim", "  curl " + "x" * cmd_len),
        ("class:trace.dim", "  · exit 0"),
    ]


def test_tool_row_clipped_to_width_at_any_size():
    for width in (50, 80, 120):
        clipped = OpenHackApp._clip_tool_row(_tool_row(), width)
        rendered = "".join(t for _, t in clipped)
        assert len(rendered) <= width, f"row overflows at width {width}"


def test_tool_row_clipping_preserves_the_outcome():
    # The command gets truncated; the result must survive — it's the bit the
    # operator actually needs.
    clipped = OpenHackApp._clip_tool_row(_tool_row(), 60)
    rendered = "".join(t for _, t in clipped)
    assert rendered.endswith("· exit 0")
    assert "…" in rendered


def test_short_tool_row_is_untouched():
    frags = _tool_row(cmd_len=5)
    assert OpenHackApp._clip_tool_row(frags, 200) == frags


def test_user_band_pad_fills_the_row():
    s = _state()
    _add(s, "you", "user", "hello")
    frags = s.trace_lines[0][1]
    used = sum(len(t) for _, t in frags)
    pad = OpenHackApp._user_band_pad(frags, 100)
    assert (used + pad) % 100 == 0        # band covers the full row
    assert 0 < pad < 100


def test_user_band_pad_handles_wrapped_messages():
    s = _state()
    _add(s, "you", "user", "x" * 250)
    frags = s.trace_lines[0][1]
    used = sum(len(t) for _, t in frags)
    pad = OpenHackApp._user_band_pad(frags, 100)
    assert (used + pad) % 100 == 0        # last wrapped row filled too


def test_non_user_lines_get_no_padding():
    s = _state()
    _add(s, "openhack", "thinking", "just prose")
    assert OpenHackApp._user_band_pad(s.trace_lines[-1][1], 100) == 0


def test_long_interactive_answer_is_rendered_in_full():
    s = _state()
    answer = "start\n" + ("full operator answer " * 300) + "\nfinal marker"

    _add(s, "openhack", "thinking", answer)

    rendered = _text(s)[0]
    assert answer in rendered
    assert "final marker" in rendered
    assert not rendered.endswith("…")


def test_long_pipeline_progress_remains_bounded():
    s = _state()
    progress = "internal progress " * 300

    _add(s, "hunter:auth", "thinking", progress)

    rendered = _text(s)[0]
    assert len(rendered) < len(progress)
    assert rendered.endswith("…")
