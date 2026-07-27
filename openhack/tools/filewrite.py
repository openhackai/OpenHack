"""Writing files — the missing half of the agent's filesystem access.

Without this the only way for the agent to create a file (an exploit PoC, a
payload, a helper script) is a `run_command` heredoc. That fails badly for
anything sizeable: the whole file body has to fit inside the tool-call JSON, so
a large script blows past the model's max_tokens, the arguments arrive
truncated, `json.loads` fails, and the call lands with empty args. The model
then retries the same doomed heredoc — a loop that burned four minutes and ~1M
tokens on session cd7d02b8 before going nowhere.

`write_file` sidesteps the size problem entirely by supporting append, so a big
file can be built in several bounded calls. Writes are jailed to the session
root exactly like reads (same `_resolve_safe_path`), and this tool is exposed
only on the agent tier — the scan pipeline stays strictly read-only.
"""

from pathlib import Path
from typing import Optional

from openhack.tools.filesystem import FileSystemTools

__all__ = ["FileWriteTools"]


class FileWriteTools:
    """Create/modify files inside the session root."""

    # A single call's payload still has to fit in the model's output budget;
    # this cap exists to give a clear error instead of a truncated write.
    MAX_CONTENT = 100_000

    def __init__(self, jail_dir: Path):
        # Reuse FileSystemTools' jail so write and read enforce identical rules.
        self._fs = FileSystemTools(Path(jail_dir))

    @property
    def jail_dir(self) -> Path:
        return self._fs.jail_dir

    def write_file(
        self,
        path: str,
        content: str,
        append: bool = False,
        mode: Optional[str] = None,
    ) -> dict:
        """Write text to a file inside the session root, creating parent dirs."""
        if not path or not str(path).strip():
            return {"error": "missing_path"}
        content = "" if content is None else str(content)
        if len(content) > self.MAX_CONTENT:
            return {
                "error": "content_too_large",
                "limit": self.MAX_CONTENT,
                "size": len(content),
                "note": (
                    "Write it in pieces: call write_file once for the first chunk, "
                    "then again with append=true for each following chunk."
                ),
            }
        try:
            resolved = self._fs._resolve_safe_path(path)
        except PermissionError as e:
            return {"error": str(e)}

        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            with open(resolved, "a" if append else "w", encoding="utf-8") as fp:
                fp.write(content)
            if mode:
                try:
                    resolved.chmod(int(str(mode), 8))
                except (ValueError, OSError) as e:
                    return {
                        "path": self._rel(resolved),
                        "bytes_written": len(content.encode("utf-8")),
                        "appended": bool(append),
                        "warning": f"file written but chmod {mode} failed: {e}",
                    }
        except OSError as e:
            return {"error": f"write failed: {e}"}

        try:
            total = resolved.stat().st_size
        except OSError:
            total = None
        return {
            "path": self._rel(resolved),
            "bytes_written": len(content.encode("utf-8")),
            "total_size": total,
            "appended": bool(append),
            "created": not append,
        }

    def _rel(self, resolved: Path) -> str:
        try:
            return str(resolved.relative_to(self.jail_dir))
        except ValueError:  # pragma: no cover - jail makes this unreachable
            return str(resolved)

    # -------------------------------------------------------------- tool spec

    def get_tool_definitions(self) -> list[dict]:
        return [
            {
                "name": "write_file",
                "description": (
                    "Write text to a file inside the session root (parent directories "
                    "are created automatically). Use this — not a `run_command` "
                    "heredoc — whenever you need to create a script, exploit PoC, "
                    "payload or config file. A heredoc has to carry the whole file "
                    "inside the command string, which truncates on anything sizeable "
                    "and fails. For a long file, write the first chunk then call again "
                    "with append=true for each subsequent chunk. Set mode='755' to make "
                    "a script executable."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Destination path, relative to the session root.",
                        },
                        "content": {
                            "type": "string",
                            "description": "Text to write.",
                        },
                        "append": {
                            "type": "boolean",
                            "description": (
                                "Append instead of overwriting — use for building a "
                                "large file across several calls. Default false."
                            ),
                        },
                        "mode": {
                            "type": "string",
                            "description": "Optional octal permissions, e.g. '755' for an executable script.",
                        },
                    },
                    "required": ["path", "content"],
                },
            },
        ]

    def execute_tool(self, name: str, arguments: dict) -> dict:
        import inspect

        tools = {"write_file": self.write_file}
        if name not in tools:
            return {"error": f"Unknown tool: {name}"}
        func = tools[name]
        valid = set(inspect.signature(func).parameters.keys())
        return func(**{k: v for k, v in (arguments or {}).items() if k in valid})
