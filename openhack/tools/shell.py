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


class ShellTools:
    """Run shell commands on behalf of the agent, with timeouts and output caps."""

    # Hard ceiling on captured output so a chatty tool can't blow the context
    # window. Kept deliberately tight: a verbose tool (sqlmap, nmap, a big curl)
    # re-sends its output on every subsequent agent turn, so an oversized result
    # inflates cost super-linearly. The agent is told when output was truncated
    # and can re-run with its own filtering (grep/head/-v0) for more.
    MAX_OUTPUT_CHARS = 20_000
    DEFAULT_TIMEOUT = 300  # seconds
    MAX_TIMEOUT = 3600

    def __init__(self, workdir: Optional[Path] = None):
        self.workdir = Path(workdir).resolve() if workdir else Path.cwd()

    # ------------------------------------------------------------------ tools

    def run_command(
        self,
        command: str,
        timeout: Optional[int] = None,
        workdir: Optional[str] = None,
        stdin: Optional[str] = None,
    ) -> dict:
        """Execute a shell command and return its output.

        The command is run through the system shell so pipes, redirects and
        globbing all work as the operator would expect at a terminal.
        """
        if not command or not command.strip():
            return {"error": "Empty command"}

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
            proc = subprocess.run(
                command,
                shell=True,
                cwd=str(cwd),
                input=stdin,
                capture_output=True,
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
                "note": (
                    f"Command exceeded the {effective_timeout}s timeout and was killed. "
                    "Re-run with a larger `timeout`, or narrow the command."
                ),
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
                            "description": "Max seconds to wait before killing the command (default 300, max 3600).",
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
        ]

    def execute_tool(self, name: str, arguments: dict) -> dict:
        import inspect

        tools = {
            "run_command": self.run_command,
            "which": self.which,
        }
        if name not in tools:
            return {"error": f"Unknown tool: {name}"}
        func = tools[name]
        valid = set(inspect.signature(func).parameters.keys())
        filtered = {k: v for k, v in arguments.items() if k in valid}
        return func(**filtered)
