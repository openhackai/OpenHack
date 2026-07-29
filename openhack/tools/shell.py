"""
Shell execution tool for the interactive hacking agent.

This is the foundational pentest primitive: it lets the agent drive any
command-line security tool the operator has installed — nmap, curl, httpx,
subfinder, nuclei, ffuf, sqlmap, osv-scanner, git, grep, and so on.

Unlike the filesystem tools (which are jailed to a scan target), the shell
tool intentionally has broad reach: authorized offensive security work needs
to touch the network and the wider system. It runs under the operator's own
account and privileges — OpenHack does not add a moral filter on top of tools
the operator already has. What it *does* add is operational safety: every
command runs with a timeout, output is captured and size-bounded, and the
working directory is explicit.
"""

import os
import shlex
import subprocess
from pathlib import Path
from typing import Optional

from openhack.tools.process import run_killable


class ShellTools:
    """Run shell commands on behalf of the agent, with timeouts and output caps."""

    # Hard ceiling on captured output so a chatty tool can't blow the context
    # window. Kept deliberately tight: a verbose tool (sqlmap, nmap, a big curl)
    # re-sends its output on every subsequent agent turn, so an oversized result
    # inflates cost super-linearly. The agent is told when output was truncated
    # and can re-run with its own filtering (grep/head/-v0) for more.
    MAX_OUTPUT_CHARS = 20_000

    # The DEFAULT is what an agent gets when it wasn't thinking about timeouts,
    # so it should be sized for a probe, not for the longest thing a command
    # could legitimately do. It used to be 300s, and session 9d80e4af paid for
    # it: `docker ps` and `docker info` each blocked the full five minutes
    # against a daemon that wasn't running (the docker CLI hangs on an
    # unreachable socket instead of failing), turning two throwaway probes into
    # 10 minutes — over a third of a 28.7-minute run.
    #
    # 60s still covers builds, installs and most scans. Anything genuinely
    # long-running just passes `timeout` explicitly and can go to MAX_TIMEOUT,
    # or belongs in a background shell (run_in_background) where it doesn't
    # block the agent at all.
    DEFAULT_TIMEOUT = 60  # seconds
    MAX_TIMEOUT = 3600

    # Commands whose CLIs block on an unreachable daemon rather than failing
    # fast. Hitting the timeout with one of these almost always means the
    # service is down, not that the command needed longer — so the hint has to
    # say that. The old note said "re-run with a larger timeout", which is the
    # exact wrong advice and invites the agent to burn even more time.
    _DAEMON_CLIS = ("docker", "podman", "kubectl", "colima", "minikube", "nerdctl")

    def __init__(self, workdir: Optional[Path] = None, session=None, shells=None):
        self.workdir = Path(workdir).resolve() if workdir else Path.cwd()
        # When set, spawned commands register with the session so ESC/cancel can
        # kill them immediately (see tools/process.run_killable).
        self._session = session
        # Shared ShellManager for background shells (run_in_background). Lets the
        # agent launch a listener/server/long scan and keep working; the same
        # manager backs the TUI's /bashes view. Lazily created if not supplied.
        self._shells = shells
        self._bash_cursors: dict[str, int] = {}

    @classmethod
    def _timeout_note(cls, command: str, effective_timeout: int) -> str:
        """Explain a timeout in terms of the likely cause.

        Which CLI hung matters. A blocked `docker` call means the daemon is
        unreachable and waiting longer will not help; a blocked build may
        genuinely need more time. Steering those to the same advice is what
        made session 9d80e4af retry its way through 10 minutes of dead wait.
        """
        base = f"Command exceeded the {effective_timeout}s timeout and was killed."
        lowered = command.lower()
        hit = next((c for c in cls._DAEMON_CLIS if c in lowered), None)
        if hit:
            return (
                f"{base} `{hit}` blocks instead of failing when its daemon is "
                f"unreachable, so this most likely means {hit} is not running — "
                f"a longer timeout will not help. Check it is up first (for "
                f"docker: `docker info --format '{{{{.ServerVersion}}}}'` with an "
                f"explicit short timeout), start it if not, then poll readiness "
                f"in short steps rather than sleeping for a fixed guess."
            )
        return (
            f"{base} If it was still making progress, re-run with a larger "
            f"`timeout` (up to {cls.MAX_TIMEOUT}s) or use run_in_background=True "
            f"so it doesn't block. If it was stuck, narrow the command instead."
        )

    def _ensure_shells(self):
        if self._shells is None:
            from openhack.shells import ShellManager
            self._shells = ShellManager()
        return self._shells

    # ------------------------------------------------------------------ tools

    def run_command(
        self,
        command: str,
        timeout: Optional[int] = None,
        run_in_background: bool = False,
        workdir: Optional[str] = None,
        stdin: Optional[str] = None,
    ) -> dict:
        """Execute a shell command and return its output.

        The command is run through the system shell so pipes, redirects and
        globbing all work as the operator would expect at a terminal.
        """
        if not command or not command.strip():
            return {"error": "Empty command"}

        # Background: hand off to the ShellManager and return immediately with an
        # id the agent can poll with bash_output / stop with kill_shell.
        if run_in_background:
            cwd = str(self.workdir)
            if workdir:
                candidate = Path(workdir)
                resolved = (candidate if candidate.is_absolute() else self.workdir / candidate).resolve()
                if not resolved.is_dir():
                    return {"error": f"Working directory does not exist: {workdir}"}
                cwd = str(resolved)
            try:
                sid = self._ensure_shells().spawn(command, cwd=cwd)
            except OSError as e:
                return {"error": f"Failed to start background command: {e}"}
            return {
                "shell_id": sid,
                "status": "running",
                "note": (
                    f"Started in the background as {sid}. Poll its output with "
                    f"bash_output(shell_id='{sid}') and stop it with kill_shell(shell_id='{sid}')."
                ),
            }

        effective_timeout = min(
            int(timeout) if timeout else self.DEFAULT_TIMEOUT, self.MAX_TIMEOUT
        )

        cwd = self.workdir
        if workdir:
            candidate = Path(workdir)
            if not candidate.is_absolute():
                candidate = self.workdir / candidate
            cwd = candidate.resolve()
            if not cwd.is_dir():
                return {"error": f"Working directory does not exist: {workdir}"}

        try:
            proc = run_killable(
                command,
                session=self._session,
                shell=True,
                cwd=str(cwd),
                input=stdin,
                text=True,
                timeout=effective_timeout,
                env=os.environ.copy(),
            )
        except subprocess.TimeoutExpired as e:
            partial = self._decode(e.stdout) + self._decode(e.stderr)
            return {
                "command": command,
                "timed_out": True,
                "timeout": effective_timeout,
                "output": self._cap(partial),
                "note": self._timeout_note(command, effective_timeout),
            }
        except Exception as e:  # pragma: no cover - defensive
            return {"error": f"Failed to run command: {e}"}

        stdout, out_truncated = self._cap_flag(proc.stdout or "")
        stderr, err_truncated = self._cap_flag(proc.stderr or "")

        result = {
            "command": command,
            "exit_code": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "cwd": str(cwd),
        }
        if out_truncated or err_truncated:
            result["truncated"] = True
            result["note"] = (
                "Output was truncated. Re-run piping through grep/head/tail, "
                "or write to a file and read it in slices."
            )
        return result

    def which(self, tool: str) -> dict:
        """Check whether a command-line tool is installed and on PATH."""
        if not tool or not tool.strip():
            return {"error": "No tool name given"}
        # Only look up the bare executable name, never a full command line.
        name = shlex.split(tool)[0] if tool.strip() else tool
        from shutil import which as _which

        path = _which(name)
        return {"tool": name, "installed": path is not None, "path": path}

    def bash_output(self, shell_id: str) -> dict:
        """Return output produced by a background shell since the last poll."""
        if self._shells is None:
            return {"error": "no background shells"}
        sh = self._shells.get(shell_id)
        if sh is None:
            return {"error": f"no such shell: {shell_id}"}
        lines, cursor = sh.since(self._bash_cursors.get(shell_id, 0))
        self._bash_cursors[shell_id] = cursor
        return {
            "shell_id": shell_id,
            "status": sh.status,
            "exit_code": sh.returncode,
            "output": self._cap("\n".join(lines)),
        }

    def kill_shell(self, shell_id: str) -> dict:
        """Terminate a background shell (SIGTERM → SIGKILL)."""
        if self._shells is None or self._shells.get(shell_id) is None:
            return {"error": f"no such shell: {shell_id}"}
        self._shells.kill(shell_id)
        return {"shell_id": shell_id, "killed": True}

    # ----------------------------------------------------------------- helpers

    def _decode(self, blob) -> str:
        if blob is None:
            return ""
        if isinstance(blob, bytes):
            return blob.decode("utf-8", errors="replace")
        return str(blob)

    def _cap(self, text: str) -> str:
        return self._cap_flag(text)[0]

    def _cap_flag(self, text: str) -> tuple[str, bool]:
        if len(text) <= self.MAX_OUTPUT_CHARS:
            return text, False
        head = self.MAX_OUTPUT_CHARS // 2
        tail = self.MAX_OUTPUT_CHARS - head
        clipped = (
            text[:head]
            + f"\n\n... [{len(text) - self.MAX_OUTPUT_CHARS:,} chars truncated] ...\n\n"
            + text[-tail:]
        )
        return clipped, True

    # -------------------------------------------------------------- tool specs

    def get_tool_definitions(self) -> list[dict]:
        return [
            {
                "name": "run_command",
                "description": (
                    "Run a shell command and get its stdout, stderr and exit code. "
                    "Use this to drive any installed CLI security tool (nmap, curl, "
                    "httpx, subfinder, nuclei, ffuf, sqlmap, osv-scanner, git, etc.), "
                    "inspect the system, or chain tools with pipes. Runs through the "
                    "system shell, so pipes, redirects and globs work. Always prefer "
                    "fast, non-interactive flags and avoid commands that block on input."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "The shell command line to execute.",
                        },
                        "timeout": {
                            "type": "integer",
                            "description": (
                                "Max seconds to wait before killing the command "
                                "(default 60, max 3600). Pass a SMALL value for probes "
                                "and health checks — a CLI that talks to a daemon "
                                "(docker, kubectl) hangs rather than failing when that "
                                "daemon is down, and burns the whole timeout. Pass a "
                                "large value only for work that genuinely runs long "
                                "(builds, installs, scans), or use run_in_background."
                            ),
                        },
                        "run_in_background": {
                            "type": "boolean",
                            "description": (
                                "Run the command in the background and return immediately with a "
                                "shell_id instead of blocking. Use for long-lived processes — a "
                                "listener, an HTTP server, a long scan — then read its output with "
                                "bash_output and stop it with kill_shell. Default false."
                            ),
                        },
                        "workdir": {
                            "type": "string",
                            "description": "Directory to run in (absolute, or relative to the session root). Defaults to the session root.",
                        },
                        "stdin": {
                            "type": "string",
                            "description": "Optional text to feed to the command's standard input.",
                        },
                    },
                    "required": ["command"],
                },
            },
            {
                "name": "which",
                "description": (
                    "Check whether a command-line tool is installed and available on "
                    "PATH before trying to use it. Returns the resolved path if found."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "tool": {
                            "type": "string",
                            "description": "The executable name to look up, e.g. 'nuclei'.",
                        },
                    },
                    "required": ["tool"],
                },
            },
            {
                "name": "bash_output",
                "description": (
                    "Read new output from a background shell (one started with "
                    "run_command run_in_background=true). Returns output produced since "
                    "your last bash_output call for that shell, plus its status and exit "
                    "code. Poll this to watch a background process make progress."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "shell_id": {
                            "type": "string",
                            "description": "The id returned by run_command run_in_background=true, e.g. 'sh1'.",
                        },
                    },
                    "required": ["shell_id"],
                },
            },
            {
                "name": "kill_shell",
                "description": "Stop a background shell started with run_command run_in_background=true.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "shell_id": {
                            "type": "string",
                            "description": "The id of the background shell to terminate, e.g. 'sh1'.",
                        },
                    },
                    "required": ["shell_id"],
                },
            },
        ]

    def execute_tool(self, name: str, arguments: dict) -> dict:
        import inspect

        tools = {
            "run_command": self.run_command,
            "which": self.which,
            "bash_output": self.bash_output,
            "kill_shell": self.kill_shell,
        }
        if name not in tools:
            return {"error": f"Unknown tool: {name}"}
        func = tools[name]
        valid = set(inspect.signature(func).parameters.keys())
        filtered = {k: v for k, v in arguments.items() if k in valid}
        return func(**filtered)
