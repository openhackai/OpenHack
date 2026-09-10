"""Background shells — launch, buffer output, watch, and kill.

Powers both the user-facing `!cmd &` / `/bashes` view and the agent's
`run_in_background` / `bash_output` / `kill_shell` tools. One ShellManager is
shared between the TUI and the agent so `/bashes` lists both.

Each shell runs as a Popen in its own process group (``start_new_session=True``)
with a daemon reader thread draining stdout+stderr into a bounded, lock-guarded
line buffer. Killing signals the whole group (SIGTERM, then SIGKILL on
survivors), reusing the same primitive as the foreground kill path.
"""

from __future__ import annotations

import atexit
import os
import signal
import subprocess
import threading
import time
from typing import Optional

from openhack.tools.process import kill_process_group

__all__ = ["BackgroundShell", "ShellManager"]

_MAX_LINES = 20_000   # hard cap per shell
_TRIM_TO = 10_000     # after the cap, keep this many most-recent lines


class BackgroundShell:
    """A single backgrounded command + its rolling output buffer."""

    def __init__(self, sid: str, command: str, proc: "subprocess.Popen") -> None:
        self.id = sid
        self.command = command
        self.proc = proc
        self.status = "running"          # running | exited | killed
        self.returncode: Optional[int] = None
        self.started_at = time.time()
        self._lines: list[str] = []
        self._offset = 0                 # global index of _lines[0] after trims
        self._lock = threading.Lock()

    def append(self, line: str) -> None:
        with self._lock:
            self._lines.append(line)
            if len(self._lines) > _MAX_LINES:
                drop = len(self._lines) - _TRIM_TO
                del self._lines[:drop]
                self._offset += drop

    def tail(self, n: int = 200) -> list[str]:
        with self._lock:
            return list(self._lines[-n:])

    def since(self, cursor: int) -> tuple[list[str], int]:
        """Return (new lines with global index >= cursor, updated cursor)."""
        with self._lock:
            total = self._offset + len(self._lines)
            start = max(cursor, self._offset)
            lines = self._lines[start - self._offset:] if start <= total else []
            return list(lines), total

    def total_lines(self) -> int:
        with self._lock:
            return self._offset + len(self._lines)

    def is_running(self) -> bool:
        return self.status == "running"


class ShellManager:
    """Owns all background shells for a session/app."""

    def __init__(self) -> None:
        self._shells: dict[str, BackgroundShell] = {}
        self._counter = 0
        self._lock = threading.Lock()
        # Backstop teardown: guarantees background shells started via non-TUI
        # paths (CLI `hack`/`agent`, dispatched specialists — whose ToolRegistry
        # gets a lazily-created manager) are killed at interpreter exit, not just
        # the TUI's explicit shutdown().
        atexit.register(self.shutdown)

    def spawn(self, command: str, cwd: Optional[str] = None,
              env: Optional[dict] = None) -> str:
        with self._lock:
            self._counter += 1
            sid = f"sh{self._counter}"
        proc = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=cwd or os.getcwd(),
            env=env,
            start_new_session=True,   # own process group → group-kill takes the tree
        )
        sh = BackgroundShell(sid, command, proc)
        with self._lock:
            self._shells[sid] = sh
        t = threading.Thread(target=self._reader, args=(sh,), daemon=True)
        t.start()
        return sid

    def _reader(self, sh: BackgroundShell) -> None:
        try:
            for line in sh.proc.stdout:  # blocks until EOF (process exit / kill)
                sh.append(line.rstrip("\n"))
        except Exception:
            pass
        finally:
            try:
                sh.returncode = sh.proc.wait()
            except Exception:
                sh.returncode = None
            if sh.status != "killed":
                sh.status = "exited"

    def list(self) -> list[BackgroundShell]:
        with self._lock:
            return list(self._shells.values())

    def get(self, sid: str) -> Optional[BackgroundShell]:
        with self._lock:
            return self._shells.get(sid)

    def kill(self, sid: str) -> bool:
        sh = self.get(sid)
        if sh is None:
            return False
        if sh.status == "running":
            sh.status = "killed"
            kill_process_group(sh.proc, signal.SIGTERM)

            def _hard() -> None:
                try:
                    if sh.proc.poll() is None:
                        kill_process_group(sh.proc)  # hard kill (SIGKILL on Unix)
                except OSError:
                    pass

            t = threading.Timer(0.4, _hard)
            t.daemon = True
            t.start()
        return True

    def kill_all(self) -> None:
        for sh in self.list():
            self.kill(sh.id)

    def shutdown(self, grace: float = 0.4) -> None:
        """Blocking teardown for quit/exit: SIGTERM every running shell, wait up
        to `grace`, then SIGKILL survivors synchronously. Unlike kill()'s daemon
        Timer, this can't be abandoned by interpreter finalization — so a
        SIGTERM-resistant child is still force-killed before we exit."""
        procs = [sh for sh in self.list() if sh.is_running()]
        for sh in procs:
            sh.status = "killed"
            kill_process_group(sh.proc, signal.SIGTERM)
        if not procs:
            return
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if all(sh.proc.poll() is not None for sh in procs):
                return
            time.sleep(0.02)
        for sh in procs:
            try:
                if sh.proc.poll() is None:
                    kill_process_group(sh.proc)  # hard kill (SIGKILL on Unix)
            except OSError:
                pass
