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
