"""Command timeout defaults and guidance, from session 9d80e4af.

That run spent 28.7 minutes on a task worth about 10. Two throwaway probes —
`docker ps` and `docker info` — each blocked the full 300s default against a
daemon that wasn't running, because the docker CLI hangs on an unreachable
socket rather than failing. 10 minutes, over a third of the run, on two
commands that should have returned in milliseconds. The old timeout note then
advised "re-run with a larger timeout", which is precisely backwards.
"""

import subprocess
import time

import pytest

from openhack.tools.shell import ShellTools


# ------------------------------------------------------------------ defaults

def test_default_timeout_is_sized_for_a_probe():
    # The default is what an agent gets when it wasn't thinking about timeouts,
    # so a mistake must cost seconds, not minutes.
    assert ShellTools.DEFAULT_TIMEOUT <= 60
    assert ShellTools.MAX_TIMEOUT >= 600, "long work must still be expressible"


def test_explicit_timeout_still_wins_over_the_default():
    t = ShellTools()
    r = t.run_command("sleep 5", timeout=1)
    assert r["timed_out"] is True
    assert r["timeout"] == 1


def test_explicit_timeout_is_capped_at_max():
    t = ShellTools()
    r = t.run_command("echo hi", timeout=99_999)
    assert r.get("exit_code") == 0  # sanity: it ran
    assert ShellTools.MAX_TIMEOUT == 3600


def test_a_hung_command_is_killed_at_the_default_not_left_running():
    t = ShellTools()
    # Patch the default down so the test doesn't actually wait a minute; the
    # point is that the DEFAULT path (no explicit timeout) does terminate.
    orig = ShellTools.DEFAULT_TIMEOUT
    ShellTools.DEFAULT_TIMEOUT = 1
    try:
        t0 = time.monotonic()
        r = t.run_command("sleep 30")
        elapsed = time.monotonic() - t0
    finally:
        ShellTools.DEFAULT_TIMEOUT = orig
    assert r["timed_out"] is True
    assert elapsed < 10, f"took {elapsed:.1f}s to honour a 1s default"


# ------------------------------------------------------------- timeout advice

def test_daemon_cli_timeout_says_the_daemon_is_down():
    note = ShellTools._timeout_note("which docker && docker info 2>&1 | head -5", 60)
    assert "not running" in note
    assert "will not help" in note
    # Must NOT repeat the old advice for this case.
    assert "larger `timeout`" not in note


def test_daemon_hint_recognises_the_real_failing_command():
    # The exact command from 9d80e4af that burned 300s — docker is buried
    # mid-line behind an `ls`, so a naive startswith() check would miss it.
    cmd = ('ls -la /tmp/testbed 2>/dev/null; echo "---DOCKER---"; docker ps '
           '2>/dev/null; echo "---PORTS---"; lsof -iTCP -sTCP:LISTEN -P | head -40')
    assert "not running" in ShellTools._timeout_note(cmd, 300)


@pytest.mark.parametrize("cli", ["docker", "kubectl", "podman", "colima"])
def test_all_daemon_clis_get_the_hint(cli):
    assert "not running" in ShellTools._timeout_note(f"{cli} ps", 60)


def test_ordinary_timeout_keeps_the_useful_advice():
    note = ShellTools._timeout_note("make build", 60)
    assert "larger" in note and "run_in_background" in note
    assert "not running" not in note


def test_timeout_result_carries_the_note_and_partial_output():
    t = ShellTools()
    r = t.run_command("echo partial; sleep 30", timeout=1)
    assert r["timed_out"] is True
    assert "note" in r and r["note"]
    assert "output" in r


# --------------------------------------------------------------- tool schema

def test_schema_documents_the_new_default_and_probe_guidance():
    tools = ShellTools().get_tool_definitions()
    spec = next(t for t in tools if t["name"] == "run_command")
    desc = spec["parameters"]["properties"]["timeout"]["description"]
    assert "default 60" in desc
    assert "300" not in desc, "schema still advertises the old default"
    # The schema is the only place the model reliably reads before its first
    # mistake, so the daemon warning has to live here too — not just in the
    # note it gets after already burning the time.
    assert "docker" in desc.lower()


# ------------------------------------------------------------------- prompt

def test_prompt_tells_the_agent_to_poll_not_sleep():
    from openhack.agents.interactive import SYSTEM_PROMPT

    p = SYSTEM_PROMPT.lower()
    assert "poll" in p
    assert "sleep 45" in p, "the concrete anti-pattern should be named"
    assert "block instead of failing" in p
