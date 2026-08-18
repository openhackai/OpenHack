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
from typing import Optional, Sequence, Union

__all__ = ["run_killable", "kill_process_group", "process_group_kwargs"]

# Never let a command's byte stream inherit the host's locale codec. On
# Windows that is commonly cp1252, whose undefined bytes (including 0x81)
# raise inside subprocess._readerthread and silently cut off the tool result.
# Security tools overwhelmingly emit UTF-8; replacement keeps malformed or
# legacy-encoded output visible without killing the reader.
_OUTPUT_ENCODING = "utf-8"
_OUTPUT_ERRORS = "replace"


def process_group_kwargs() -> dict:
    """Return the Popen options that isolate a child tree on this platform."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _is_running(proc) -> bool:
    poll = getattr(proc, "poll", None)
    if callable(poll):
        return poll() is None
    return getattr(proc, "returncode", None) is None


def kill_process_group(proc: "subprocess.Popen", sig: Optional[int] = None) -> None:
    """Terminate the child's whole process tree on Windows or POSIX."""
    if not _is_running(proc):
        return

    if os.name == "nt":
        # Windows has no killpg/SIGKILL. taskkill /T is the native tree-kill
        # primitive; /F makes cancellation immediate for console tools that do
        # not have a cooperative close path. Fall back to the process handle if
        # taskkill is unavailable or blocked by policy.
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError):
            pass
        if _is_running(proc):
            try:
                proc.kill()
            except (OSError, ProcessLookupError):
                pass
        return

    effective_signal = sig if sig is not None else signal.SIGKILL
    try:
        os.killpg(os.getpgid(proc.pid), effective_signal)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.send_signal(effective_signal)
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
    text_options = (
        {
            "text": True,
            "encoding": _OUTPUT_ENCODING,
            "errors": _OUTPUT_ERRORS,
        }
        if text
        else {"text": False}
    )
    proc = subprocess.Popen(
        cmd,
        shell=shell,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE if input is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **text_options,
        # Put the child in an isolated process group so cancellation can take
        # down a shell pipeline or wrapped scanner without orphaning children.
        **process_group_kwargs(),
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
