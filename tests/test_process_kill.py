"""Interruptible subprocess execution: cancel/ESC must kill a running tool now.

The lag being fixed: tools run in a worker thread (asyncio.to_thread) and a
Python thread can't be preempted, so a blocking command couldn't be stopped
until it finished. run_killable() spawns the child in its own process group and
registers it with the Session, so Session.cancel() signal-kills it immediately —
which unblocks the waiting thread. These tests prove the kill actually lands.
"""

import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from openhack.agents.session import Session
from openhack.tools.process import (
    kill_process_group,
    process_group_kwargs,
    run_killable,
)
from openhack.tools.shell import ShellTools


def _sleep_args(seconds=30):
    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


def _shell_command(args):
    return subprocess.list2cmdline(args) if os.name == "nt" else shlex.join(args)


def test_run_killable_returns_completed_process():
    r = run_killable([sys.executable, "-c", "print('hi')"], timeout=5)
    assert r.returncode == 0
    assert "hi" in r.stdout


def test_run_killable_replaces_undecodable_output_instead_of_losing_response():
    code = (
        "import os; "
        "os.write(1, b'before' + bytes([0x81]) + b'after'); "
        "os.write(2, b'error' + bytes([0xff]) + b'tail')"
    )

    result = run_killable([sys.executable, "-c", code], timeout=5)

    assert result.returncode == 0
    assert result.stdout == "before\ufffdafter"
    assert result.stderr == "error\ufffdtail"


def test_run_killable_registers_then_unregisters():
    calls = []

    class Rec:
        def register_process(self, p):
            calls.append("reg")

        def unregister_process(self, p):
            calls.append("unreg")

    run_killable([sys.executable, "-c", "pass"], session=Rec(), timeout=5)
    assert calls == ["reg", "unreg"]  # always cleaned up


def test_run_killable_unregisters_even_on_timeout():
    calls = []

    class Rec:
        def register_process(self, p):
            calls.append("reg")

        def unregister_process(self, p):
            calls.append("unreg")

    with pytest.raises(subprocess.TimeoutExpired):
        run_killable(_sleep_args(10), session=Rec(), timeout=0.3)
    assert calls == ["reg", "unreg"]  # killed + cleaned up, didn't hang 10s


def test_child_is_process_group_leader_and_killable():
    proc = subprocess.Popen(_sleep_args(), **process_group_kwargs())
    try:
        if os.name != "nt":
            # start_new_session makes the child its own process-group leader.
            assert os.getpgid(proc.pid) == proc.pid
        kill_process_group(proc, signal.SIGTERM)
        assert _wait_dead(proc), "kill_process_group did not stop the child"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_session_cancel_kills_registered_process(tmp_path):
    session = Session(target_dir=str(tmp_path))
    proc = subprocess.Popen(_sleep_args(), **process_group_kwargs())
    session.register_process(proc)
    try:
        session.cancel()
        assert _wait_dead(proc), "session.cancel() did not kill the process"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    # And a killed process is unregistered by whoever spawned it; here we did it
    # manually, so just confirm the registry can be cleared without error.
    session.unregister_process(proc)


def test_run_command_stops_immediately_on_cancel(tmp_path):
    # The money test: a long command driven through the shell tool is killed the
    # instant the session is cancelled — not after it finishes on its own.
    session = Session(target_dir=str(tmp_path))
    sh = ShellTools(workdir=tmp_path, session=session)
    box = {}

    def run():
        box["result"] = sh.run_command(_shell_command(_sleep_args()), timeout=60)

    t = threading.Thread(target=run)
    t.start()
    time.sleep(0.5)  # let the command start and register

    start = time.monotonic()
    session.cancel()
    t.join(timeout=5)
    elapsed = time.monotonic() - start

    assert not t.is_alive(), "run_command did not return after cancel"
    assert elapsed < 3, f"took {elapsed:.1f}s to stop — should be immediate"
    assert box["result"].get("exit_code", 0) != 0  # process was killed


def test_run_killable_honours_already_cancelled_session(tmp_path):
    # TOCTOU: if cancel() fired in the spawn/register gap (so the kill snapshot
    # missed this child), run_killable must honour the pending cancel and kill
    # the just-spawned process instead of blocking on it for the full timeout.
    session = Session(target_dir=str(tmp_path))
    session.cancelled = True  # cancel already happened
    start = time.monotonic()
    r = run_killable(_sleep_args(), session=session, timeout=60)
    elapsed = time.monotonic() - start
    assert elapsed < 3, f"did not honour pending cancel (took {elapsed:.1f}s)"
    assert r.returncode != 0  # killed, not a clean exit


def _wait_dead(proc, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return True
        time.sleep(0.02)
    return proc.poll() is not None
