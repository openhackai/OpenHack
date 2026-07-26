"""Foreground `!` bang mode: dispatch routing + streaming/interrupt of _run_shell."""

import asyncio
import time

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
