"""Background-shell manager: spawn, buffer, poll, and kill."""

import time

from openhack.shells import ShellManager


def _wait(pred, timeout=4.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.03)
    return pred()


def test_spawn_buffers_output_and_exits():
    mgr = ShellManager()
    sid = mgr.spawn("printf 'alpha\\nbeta\\n'")
    sh = mgr.get(sid)
    assert sh is not None and sid == "sh1"
    assert _wait(lambda: sh.status == "exited"), "shell never exited"
    lines = sh.tail(10)
    assert "alpha" in lines and "beta" in lines
    assert sh.returncode == 0


def test_since_cursor_polling_returns_only_new_lines():
    mgr = ShellManager()
    sid = mgr.spawn("printf 'one\\ntwo\\nthree\\n'")
    sh = mgr.get(sid)
    assert _wait(lambda: sh.status == "exited")
    first, cursor = sh.since(0)
    assert first == ["one", "two", "three"]
    more, cursor2 = sh.since(cursor)
    assert more == [] and cursor2 == cursor  # nothing new since last poll


def test_kill_terminates_a_running_shell():
    mgr = ShellManager()
    sid = mgr.spawn("sleep 30")
    sh = mgr.get(sid)
    assert _wait(lambda: sh.is_running(), timeout=1.0)
    assert mgr.kill(sid) is True
    assert sh.status == "killed"
    assert _wait(lambda: sh.proc.poll() is not None), "process was not killed"


def test_kill_all():
    mgr = ShellManager()
    a = mgr.spawn("sleep 30")
    b = mgr.spawn("sleep 30")
    for sid in (a, b):
        assert _wait(lambda s=sid: mgr.get(s).is_running(), timeout=1.0)
    mgr.kill_all()
    assert _wait(lambda: all(mgr.get(s).proc.poll() is not None for s in (a, b)))


def test_shutdown_kills_running_shells_synchronously():
    mgr = ShellManager()
    sid = mgr.spawn("sleep 30")
    assert _wait(lambda: mgr.get(sid).is_running(), timeout=1.0)
    mgr.shutdown()  # blocking: must not return until the child is dead
    assert mgr.get(sid).proc.poll() is not None


def test_ids_are_sequential_and_listed():
    mgr = ShellManager()
    ids = [mgr.spawn("true") for _ in range(3)]
    assert ids == ["sh1", "sh2", "sh3"]
    assert {s.id for s in mgr.list()} == {"sh1", "sh2", "sh3"}
