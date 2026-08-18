"""
File system tools for vulnerability scanning.
Provides safe, jailed access to the target directory.
"""

import fnmatch
import hashlib
import inspect
import os
import re
from pathlib import Path
from typing import Optional

_GREP_EXCLUDE_DIRS = [
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".next", ".nuxt", ".output",
    "vendor", "target", "coverage", ".mypy_cache",
    ".pytest_cache", ".tox", "eggs", "*.egg-info",
]

_GREP_SOURCE_INCLUDES = [
    "*.py", "*.js", "*.ts", "*.tsx", "*.jsx",
    "*.rb", "*.go", "*.rs", "*.java", "*.php",
    "*.c", "*.cpp", "*.h",
    "*.vue", "*.svelte",
]


def _walk_matching_files(root: Path, includes: list[str]):
    """Yield matching files without relying on platform-specific shell tools."""
    for current_root, dirs, files in os.walk(root):
        dirs[:] = [
            name for name in dirs
            if not any(fnmatch.fnmatch(name, pattern) for pattern in _GREP_EXCLUDE_DIRS)
        ]
        for name in files:
            if any(fnmatch.fnmatch(name, pattern) for pattern in includes):
                yield Path(current_root) / name


class FileSystemTools:
    """File system tools with path safety enforcement."""

    def __init__(self, jail_dir: Path, unrestricted: bool = False):
        self.jail_dir = jail_dir.resolve()
        self.unrestricted = unrestricted

    def _resolve_safe_path(self, path: str) -> Path:
        """Resolve a path, enforcing the target jail unless explicitly disabled."""
        raw = Path(path).expanduser()
        requested = raw.resolve() if raw.is_absolute() else (self.jail_dir / raw).resolve()
        if not self.unrestricted and not requested.is_relative_to(self.jail_dir):
            raise PermissionError(f"Access denied: {path} is outside the allowed directory")
        return requested

    def _display_path(self, path: Path) -> str:
        try:
            # Tool paths are an internal, portable contract.  Keep them in POSIX
            # form even when the scanner itself is running on Windows.
            return path.relative_to(self.jail_dir).as_posix()
        except ValueError:
            return str(path)

    def _file_metadata(self, path: Path) -> dict:
        stat = path.stat()
        digest = hashlib.sha256()
        with open(path, "rb") as fp:
            for block in iter(lambda: fp.read(1024 * 1024), b""):
                digest.update(block)
        return {
            "resolved_path": str(path),
            "size_bytes": stat.st_size,
            "modified_at": stat.st_mtime,
            "sha256": digest.hexdigest(),
            "access_scope": "unrestricted" if self.unrestricted else "target_jail",
        }

    BINARY_EXTENSIONS = frozenset({
        ".zip", ".gz", ".tar", ".bz2", ".xz", ".7z", ".rar",
        ".jar", ".war", ".ear", ".class",
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".svg",
        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
        ".woff", ".woff2", ".ttf", ".eot", ".otf",
        ".exe", ".dll", ".so", ".dylib", ".o", ".a",
        ".pyc", ".pyo", ".wasm",
        ".sqlite", ".db", ".sqlite3",
        ".mp3", ".mp4", ".avi", ".mov", ".wav", ".flac",
        ".bin", ".dat", ".iso", ".img",
        ".sql", ".csv", ".tsv", ".log", ".dump",
    })
    MAX_FILE_SIZE = 200_000  # 200KB — files larger than this are truncated
    MAX_LINES_DEFAULT = 1000

    def read_file(self, path: str, offset: int = 0, limit: Optional[int] = None) -> dict:
        """Read the contents of a file with line numbers."""
        try:
            resolved = self._resolve_safe_path(path)
            if not resolved.exists():
                return {"error": f"File not found: {path}"}
            if not resolved.is_file():
                return {"error": f"Not a file: {path}"}

            if resolved.suffix.lower() in self.BINARY_EXTENSIONS:
                size = resolved.stat().st_size
                return {
                    "path": self._display_path(resolved),
                    "content": f"[Binary file: {resolved.suffix}, {size:,} bytes — cannot read]",
                    "total_lines": 0,
                    "binary": True,
                    **self._file_metadata(resolved),
                }

            file_size = resolved.stat().st_size
            if file_size > self.MAX_FILE_SIZE:
                effective_limit = limit or self.MAX_LINES_DEFAULT
            else:
                effective_limit = limit

            if effective_limit and file_size > self.MAX_FILE_SIZE:
                with open(resolved, "r", encoding="utf-8", errors="replace") as f:
                    lines = []
                    for _ in range(offset):
                        if not f.readline():
                            break
                    for _ in range(effective_limit):
                        line = f.readline()
                        if not line:
                            break
                        lines.append(line)
                    remaining = sum(1 for _ in f)
                total_lines = offset + len(lines) + remaining
                was_truncated = remaining > 0
            else:
                with open(resolved, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                total_lines = len(lines)
                if effective_limit:
                    lines = lines[offset : offset + effective_limit]
                    was_truncated = (offset + effective_limit) < total_lines
                else:
                    lines = lines[offset:]
                    was_truncated = False

            numbered_lines = []
            for i, line in enumerate(lines, start=offset + 1):
                numbered_lines.append(f"{i:6}\t{line.rstrip()}")

            if was_truncated and file_size > self.MAX_FILE_SIZE:
                numbered_lines.append(f"\n[... file truncated: {total_lines:,} total lines, {file_size:,} bytes — use offset/limit to read more ...]")

            return {
                "path": self._display_path(resolved),
                "content": "\n".join(numbered_lines),
                "total_lines": total_lines,
                "offset": offset,
                "lines_returned": len(numbered_lines),
                **self._file_metadata(resolved),
            }
        except PermissionError as e:
            return {
                "error": str(e),
                "requested_path": path,
                "access_scope": "unrestricted" if self.unrestricted else "target_jail",
            }
        except Exception as e:
            return {"error": f"Error reading file: {e}"}

    def list_dir(self, path: str = ".", ignore: Optional[list[str]] = None) -> dict:
        """List contents of a directory."""
        try:
            resolved = self._resolve_safe_path(path)
            if not resolved.exists():
                return {"error": f"Directory not found: {path}"}
            if not resolved.is_dir():
                return {"error": f"Not a directory: {path}"}

            ignore = ignore or []
            entries = []
            for entry in sorted(resolved.iterdir()):
                rel_path = self._display_path(entry)
                if any(fnmatch.fnmatch(rel_path, pat) or fnmatch.fnmatch(entry.name, pat) for pat in ignore):
                    continue
                entry_type = "dir" if entry.is_dir() else "file"
                size = entry.stat().st_size if entry.is_file() else None
                entries.append({"name": entry.name, "type": entry_type, "size": size})

            return {
                "path": self._display_path(resolved),
                "entries": entries,
            }
        except PermissionError as e:
            return {"error": str(e)}
        except Exception as e:
            return {"error": f"Error listing directory: {e}"}

    def _expand_braces(self, pattern: str) -> list[str]:
        """Expand brace patterns like {js,jsx,ts,tsx} into multiple patterns."""
        import re
        brace_pattern = re.compile(r'\{([^}]+)\}')
        match = brace_pattern.search(pattern)
        if not match:
            return [pattern]
        
        prefix = pattern[:match.start()]
        suffix = pattern[match.end():]
        alternatives = match.group(1).split(',')
        
        expanded = []
        for alt in alternatives:
            expanded.extend(self._expand_braces(prefix + alt.strip() + suffix))
        return expanded

    def glob(self, pattern: str, path: str = ".") -> dict:
        """Find files matching a glob pattern recursively.

        Supports brace expansion like {js,jsx,ts,tsx}.
        Skips known non-source directories for performance.
        """
        try:
            resolved = self._resolve_safe_path(path)
            if not resolved.exists():
                return {"error": f"Directory not found: {path}"}

            matches = set()
            skip_dirs = {d.rstrip("*").rstrip(".") for d in _GREP_EXCLUDE_DIRS}

            expanded_patterns = self._expand_braces(pattern)

            for exp_pattern in expanded_patterns:
                search_pattern = exp_pattern
                recursive = False
                if search_pattern.startswith("**/"):
                    search_pattern = search_pattern[3:]
                    recursive = True

                match_path = "/" in search_pattern

                for root, dirs, files in os.walk(resolved):
                    dirs[:] = [d for d in dirs if d not in skip_dirs]
                    for f in files:
                        matched = False
                        if match_path:
                            full = Path(root) / f
                            rel_from_base = full.relative_to(resolved).as_posix()
                            # Check exact match or any path suffix
                            if fnmatch.fnmatch(rel_from_base, search_pattern):
                                matched = True
                            elif recursive:
                                parts = rel_from_base.split("/")
                                for i in range(len(parts)):
                                    suffix = "/".join(parts[i:])
                                    if fnmatch.fnmatch(suffix, search_pattern):
                                        matched = True
                                        break
                        else:
                            matched = fnmatch.fnmatch(f, search_pattern)

                        if matched:
                            full = Path(root) / f if not match_path else full
                            rel_path = self._display_path(full)
                            matches.add(rel_path)
                            if len(matches) >= 500:
                                return {"pattern": pattern, "matches": sorted(matches)}

            return {"pattern": pattern, "matches": sorted(matches)[:500]}
        except PermissionError as e:
            return {"error": str(e)}
        except Exception as e:
            return {"error": f"Error during glob: {e}"}

    def grep(self, pattern: str, path: str = ".", include: Optional[str] = None) -> dict:
        """Search for a regex pattern without requiring an external grep binary."""
        try:
            resolved = self._resolve_safe_path(path)
            if not resolved.exists():
                return {"error": f"Path not found: {path}"}

            if resolved.is_file():
                return self._grep_single_file(resolved, pattern)

            regex = re.compile(pattern, re.IGNORECASE)
            includes = self._expand_braces(include) if include else _GREP_SOURCE_INCLUDES
            matches = []
            for file_path in _walk_matching_files(resolved, includes):
                try:
                    with open(file_path, "r", encoding="utf-8", errors="replace") as fp:
                        if not any(regex.search(line) for line in fp):
                            continue
                except (OSError, UnicodeError):
                    continue
                matches.append({
                    "file": self._display_path(file_path),
                    "line": 0,
                    "content": "",
                })
                if len(matches) >= 100:
                    break

            return {"pattern": pattern, "matches": matches}
        except PermissionError as e:
            return {"error": str(e)}
        except Exception as e:
            return {"error": f"Error during grep: {e}"}

    def _grep_single_file(self, file_path: Path, pattern: str) -> dict:
        """Grep a single file using Python (for when we need line-level results)."""
        regex = re.compile(pattern, re.IGNORECASE)
        matches = []
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                for line_num, line in enumerate(f, 1):
                    if regex.search(line):
                        matches.append({
                            "file": self._display_path(file_path),
                            "line": line_num,
                            "content": line.strip()[:200],
                        })
                        if len(matches) >= 100:
                            break
        except Exception:
            pass
        return {"pattern": pattern, "matches": matches}

    def get_tool_definitions(self) -> list[dict]:
        """Return OpenAI-compatible tool definitions."""
        return [
            {
                "name": "read_file",
                "description": "Read the contents of a file. Returns line-numbered content.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path to the file (relative to target directory)",
                        },
                        "offset": {
                            "type": "integer",
                            "description": "Line number to start reading from (0-indexed)",
                            "default": 0,
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of lines to read",
                        },
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "list_dir",
                "description": "List contents of a directory.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path to directory (relative to target)",
                            "default": ".",
                        },
                        "ignore": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Glob patterns to ignore",
                        },
                    },
                },
            },
            {
                "name": "glob",
                "description": "Find files matching a glob pattern recursively.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {
                            "type": "string",
                            "description": "Glob pattern (e.g., '*.ts', '**/*.tsx')",
                        },
                        "path": {
                            "type": "string",
                            "description": "Starting directory",
                            "default": ".",
                        },
                    },
                    "required": ["pattern"],
                },
            },
            {
                "name": "grep",
                "description": "Search for a regex pattern in files.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {
                            "type": "string",
                            "description": "Regex pattern to search for",
                        },
                        "path": {
                            "type": "string",
                            "description": "Starting path",
                            "default": ".",
                        },
                        "include": {
                            "type": "string",
                            "description": "Glob pattern for files to include (e.g., '*.ts')",
                        },
                    },
                    "required": ["pattern"],
                },
            },
        ]

    def execute_tool(self, name: str, arguments: dict) -> dict:
        """Execute a tool by name with the given arguments.

        Filters out unexpected keyword arguments that the LLM may hallucinate
        (e.g., passing 'include' to glob when it only belongs on grep).
        """
        tools = {
            "read_file": self.read_file,
            "list_dir": self.list_dir,
            "glob": self.glob,
            "grep": self.grep,
        }
        if name not in tools:
            return {"error": f"Unknown tool: {name}"}

        func = tools[name]
        sig = inspect.signature(func)
        valid_params = set(sig.parameters.keys())
        filtered_args = {k: v for k, v in arguments.items() if k in valid_params}
        return func(**filtered_args)
