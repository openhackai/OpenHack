"""Foreground `!` bang mode: dispatch routing + streaming/interrupt of _run_shell."""

import asyncio
import os
import time

from prompt_toolkit.buffer import Buffer

from openhack.tui import OpenHackApp, ScanState
from openhack.agents.session import Session


def _fresh_app():
    app = OpenHackApp.__new__(OpenHackApp)
    app._invalidate = lambda: None
    app.scan = ScanState(target="/tmp")
    app.session = Session(target_dir="/tmp", on_trace=lambda e: app.scan.update_from_trace(e))
    app.agent = None
    app.is_agent_session = True
    app._interrupting = False
    app._shell_active = True
    app._shell_proc = None
    app.scan_task = None
    app.last_status_line = ""
    return app


def _trace_text(app):
    return "\n".join("".join(seg[1] for seg in frags) for _, frags in app.scan.trace_lines)


def _run(coro_fn):
    """Run an async body then briefly settle so asyncio subprocess transports
    are reaped before asyncio.run() closes the loop (avoids a GC-time
    'Event loop is closed' warning that only happens with one loop per call)."""
    async def wrap():
        result = await coro_fn()
        await asyncio.sleep(0.15)
        return result
    return asyncio.run(wrap())


def test_bang_dispatch_routes_to_start_shell():
    app = OpenHackApp.__new__(OpenHackApp)
    app._logout_armed = False
    app._verify_arm_subject = None
    calls = []
    app._start_shell = lambda cmd: calls.append(cmd)
    asyncio.run(app._handle_input("!ls -la"))
    assert calls == ["ls -la"]


def _composer_app(text="", mode="landing"):
    app = OpenHackApp.__new__(OpenHackApp)
    app.mode = mode
    app.provider = "openhack"
    app.model = "glm-5.2"
    app.input_buffer = Buffer()
    app.input_buffer.text = text
    app._shell_input_mode = False
    app._invalidate = lambda: None
    return app


def test_bang_prefix_activates_shell_composer_ui():
    app = _composer_app("!curl https://example.com")

    assert app._is_shell_input() is True
    assert app._input_box_style() == "class:input.shell.box"
    assert app._input_bar_style() == "class:input.shell.bar"
    assert "SHELL MODE" in "".join(text for _, text in app._model_line())


def test_removing_bang_restores_normal_composer_ui():
    app = _composer_app("!pwd")
    app._shell_input_mode = True
    invalidations = []
    app._invalidate = lambda: invalidations.append(True)

    app.input_buffer.text = "pwd"
    app._on_input_text_changed(app.input_buffer)

    assert app._is_shell_input() is False
    assert app._input_box_style() == "class:input.box"
    assert app._input_bar_style() == "class:input.bar"
    assert invalidations == [True]


def test_bang_after_leading_whitespace_matches_submit_dispatch():
    app = _composer_app("   !git status")
    assert app._is_shell_input() is True


def test_bang_does_not_activate_inside_picker_input():
    app = _composer_app("!openai", mode="providers")
    assert app._is_shell_input() is False


def test_prompt_after_shell_and_cd_starts_real_agent(tmp_path, monkeypatch):
    """Regression for 21aa1bd1: never route this through status-only _chat."""
    target = tmp_path / "xss2shellwp"
    target.mkdir()
    monkeypatch.chdir(tmp_path)

    app = OpenHackApp.__new__(OpenHackApp)
    app._logout_armed = False
    app._verify_arm_subject = None
    app.scan_task = None
    app.session = object()  # the durable transcript created by !mkdir
    app.agent = None
    app.is_agent_session = True
    app.mode = "scanning"
    app.active_tab = "trace"
    app.last_status_line = ""
    app._at_index = object()

    started = []
    chatted = []
    app._start_agent = lambda task: started.append((task, os.getcwd()))

    async def status_only_chat(message):
        chatted.append(message)

    app._chat = status_only_chat

    app._cmd_cd("xss2shellwp")
    asyncio.run(app._handle_input("reproduce WP2Shell safely"))

    assert started == [("reproduce WP2Shell safely", str(target))]
    assert chatted == []
    assert app.active_tab == "trace"


def test_run_shell_streams_output_and_exit_code():
    app = _fresh_app()
    _run(lambda: app._run_shell("printf 'alpha\\nbeta\\n'"))
    txt = _trace_text(app)
    assert "alpha" in txt and "beta" in txt
    assert "exit 0" in txt
    assert "exit 0" in app.last_status_line


def test_run_shell_nonzero_exit():
    app = _fresh_app()
    _run(lambda: app._run_shell("exit 3"))
    assert "exit 3" in _trace_text(app)


def test_run_shell_handles_huge_single_line():
    # A single line > asyncio's 64KB readline limit must not error or drop output
    # (chunked reads, not readline).
    app = _fresh_app()
    _run(lambda: app._run_shell("head -c 200000 /dev/zero | tr '\\0' A"))
    txt = _trace_text(app)
    assert "shell error" not in app.last_status_line
    assert "exit 0" in app.last_status_line
    assert txt.count("A") >= 200000  # the whole big line came through


def test_run_shell_interrupt_kills_fast():
    app = _fresh_app()
    app._interrupting = True

    async def go():
        t = asyncio.create_task(app._run_shell("sleep 30"))
        await asyncio.sleep(0.4)
        start = time.monotonic()
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        return time.monotonic() - start

    elapsed = _run(go)
    assert elapsed < 3, f"interrupt took {elapsed:.1f}s"
    assert "interrupted" in _trace_text(app)
    assert app._shell_proc is None
