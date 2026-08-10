"""Spawn subprocesses so an interrupt can kill them immediately.

Tools run in a worker thread (``asyncio.to_thread``), and a Python thread can't
be preempted — so a blocking ``subprocess.run()`` means ESC / cancel can't stop
the tool until it finishes on its own. ``run_killable()`` instead spawns the
child in its **own process group** and registers it with the ``Session``, so
``Session.cancel()`` (fired by ESC or ``/cancel``) can signal-kill the whole
process tree at once — the way Claude Code aborts a running command.

Killing the child makes the waiting thread return naturally (its
``communicate()`` unblocks the moment the process dies), so the agent loop
breaks at its next checkpoint immediately instead of waiting out the command.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from typing import Optional, Sequence, Union

__all__ = ["run_killable", "kill_process_group"]

# SIGKILL / process groups are Unix-only. On Windows, SIGTERM exists but
# SIGKILL does not — None means "hard kill" via TerminateProcess.
_SIGKILL = getattr(signal, "SIGKILL", None)


def kill_process_group(
    proc: "subprocess.Popen",
    sig: Optional[int] = None,
) -> None:
    """Send *sig* to the child's whole process group; fall back to the pid.

    ``sig=None`` (default) or ``SIGKILL`` is a hard kill. ``SIGTERM`` is soft.
    On Windows there are no process groups / ``SIGKILL``; soft uses
    ``terminate()`` and hard uses ``kill()``.
    """
    if proc.poll() is not None:
        return

    hard = sig is None or sig == _SIGKILL

    if sys.platform == "win32":
        try:
            if hard:
                proc.kill()
            else:
                proc.terminate()
        except OSError:
            pass
        return

    if sig is None:
        sig = signal.SIGKILL
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.send_signal(sig)
        except OSError:
            pass


def run_killable(
    cmd: Union[str, Sequence[str]],
    *,
    session=None,
    timeout: Optional[float] = None,
    shell: bool = False,
    cwd: Optional[str] = None,
    env: Optional[dict] = None,
    input: Optional[str] = None,
    text: bool = True,
) -> "subprocess.CompletedProcess":
    """Interruptible drop-in for ``subprocess.run(..., capture_output=True)``.

    The child runs in its own process group (``start_new_session=True``) and is
    registered with *session* for the duration, so
    ``Session.kill_active_processes()`` can terminate it on cancel/interrupt.

    Raises ``subprocess.TimeoutExpired`` (after killing the tree) on timeout,
    exactly like ``subprocess.run`` — existing callers' handling is unchanged.
    """
    proc = subprocess.Popen(
        cmd,
        shell=shell,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE if input is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
        # New session => the child is its own process-group leader, so killing
        # the group takes down anything it spawned (a shell pipeline, a wrapped
        # scanner) rather than orphaning grandchildren.
        start_new_session=True,
    )
    if session is not None:
        session.register_process(proc)
        # Close the spawn/register race: if cancel() fired between Popen and
        # this register, its one-shot kill snapshot missed us. cancel() sets
        # `cancelled` before it snapshots, and the lock ordering guarantees we
        # observe it here — so honour a pending cancel now instead of blocking
        # in communicate() for the whole command.
        if getattr(session, "cancelled", False):
            kill_process_group(proc)
    try:
        try:
            out, err = proc.communicate(input=input, timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_process_group(proc)
            try:
                out, err = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                out, err = ("" if text else b""), ("" if text else b"")
            raise subprocess.TimeoutExpired(cmd, timeout, output=out, stderr=err)
        return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
    finally:
        if session is not None:
            session.unregister_process(proc)
