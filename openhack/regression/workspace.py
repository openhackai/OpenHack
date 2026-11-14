"""
Revision workspaces for regression-test validation.

Validation needs two concrete trees: the vulnerable implementation (what was
scanned) and the remediated one (a fix commit or branch, or a directory
holding the fixed code). Each is materialized into a private temporary
directory that becomes the sandbox's Docker build context. The user's
checkout is never modified, and credential files are left out so neither a
generated test nor the image build can read them.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Optional

MAX_FILES = 20_000
MAX_BYTES = 300 * 1024 * 1024
GIT_TIMEOUT = 120

_EXCLUDED_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".tox", ".nox",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".openhack-evidence",
    ".ssh", ".aws", ".gnupg", ".docker", ".kube", ".azure",
}
_SENSITIVE_NAMES = {
    ".npmrc", ".pypirc", ".netrc", ".git-credentials", ".htpasswd", ".pgpass",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "credentials.json",
    "service-account.json", "secrets.json", "secrets.yml", "secrets.yaml",
}
_SENSITIVE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".kdbx")
_ENV_TEMPLATES = {".env.example", ".env.sample", ".env.template", ".env.dist", ".env.defaults"}


class WorkspaceError(Exception):
    """A revision could not be materialized for validation."""


@dataclass(frozen=True)
class RevisionSpec:
    """Where a revision's code comes from."""

    kind: str  # "worktree" | "git" | "directory"
    value: str = ""

    @classmethod
    def worktree(cls) -> "RevisionSpec":
        return cls("worktree")

    @classmethod
    def git(cls, ref: str) -> "RevisionSpec":
        return cls("git", ref)

    @classmethod
    def directory(cls, path: str | Path) -> "RevisionSpec":
        return cls("directory", str(Path(path).expanduser().resolve()))

    @classmethod
    def parse(cls, raw: str, base_dir: Optional[Path] = None) -> "RevisionSpec":
        """An existing directory path, otherwise a git revision (sha/branch/tag)."""
        raw = (raw or "").strip()
        if not raw:
            raise WorkspaceError("empty revision")
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute() and base_dir is not None:
            candidate = base_dir / candidate
        if candidate.is_dir():
            return cls.directory(candidate)
        return cls.git(raw)

    def describe(self) -> str:
        if self.kind == "worktree":
            return "working tree"
        if self.kind == "git":
            return f"git revision `{self.value}`"
        return f"directory {self.value}"


@dataclass
class MaterializedRevision:
    spec: RevisionSpec
    root: Path
    resolved: str
    fingerprint: str
    file_count: int

    def exists(self, relpath: str) -> bool:
        return (self.root / relpath).exists()

    @property
    def label(self) -> str:
        if self.spec.kind == "git":
            short = self.resolved[:12]
            # A revision given as a (possibly abbreviated) sha needs no repeat.
            if self.resolved.startswith(self.spec.value):
                return short
            return f"{self.spec.value} ({short})"
        return self.spec.describe()


def is_excluded_path(relpath: str) -> bool:
    """Paths never copied into a validation workspace."""
    parts = PurePosixPath(relpath).parts
    if not parts:
        return True
    for part in parts[:-1]:
        if part in _EXCLUDED_DIRS or part.endswith(".egg-info"):
            return True
    name = parts[-1]
    lowered = name.lower()
    if name.startswith(".openhack-"):
        return True
    if lowered == ".env" or (lowered.startswith(".env.") and lowered not in _ENV_TEMPLATES):
        return True
    if lowered in _SENSITIVE_NAMES or lowered.startswith(("id_rsa", "id_ed25519")):
        return True
    return lowered.endswith(_SENSITIVE_SUFFIXES)


def _git(target: Path, *args: str, timeout: int = GIT_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(target), *args],
        capture_output=True, text=True, timeout=timeout, check=False,
    )


def git_info(target: Path) -> dict:
    """Repository facts for ``target`` (which may be a repo subdirectory)."""
    try:
        top = _git(target, "rev-parse", "--show-toplevel", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return {"is_repo": False}
    if top.returncode != 0:
        return {"is_repo": False}
    prefix = _git(target, "rev-parse", "--show-prefix", timeout=10).stdout.strip()
    head = _git(target, "rev-parse", "HEAD", timeout=10)
    status = _git(target, "status", "--porcelain", "--untracked-files=no", timeout=30)
    return {
        "is_repo": True,
        "toplevel": top.stdout.strip(),
        "prefix": prefix.rstrip("/"),
        "head": head.stdout.strip() if head.returncode == 0 else "",
        "dirty": bool(status.stdout.strip()),
    }


def resolve_commit(target: Path, ref: str) -> str:
    try:
        result = _git(target, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        raise WorkspaceError(f"git is unavailable: {exc}") from exc
    sha = result.stdout.strip()
    if result.returncode != 0 or not sha:
        raise WorkspaceError(f"`{ref}` is not a commit in {target}")
    return sha


class _Copier:
    """Copies files into a workspace while enforcing limits and hashing content."""

    def __init__(self, dest: Path):
        self.dest = dest.resolve()
        self.count = 0
        self.bytes = 0
        self.entries: list[tuple[str, str]] = []

    def _target(self, relpath: str) -> Path:
        path = (self.dest / relpath).resolve()
        if not path.is_relative_to(self.dest):
            raise WorkspaceError(f"refusing to write outside the workspace: {relpath}")
        return path

    def add(self, relpath: str, stream, executable: bool = False) -> None:
        self.count += 1
        if self.count > MAX_FILES:
            raise WorkspaceError(f"revision has more than {MAX_FILES} files")
        target = self._target(relpath)
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        with open(target, "wb") as out:
            while True:
                block = stream.read(1024 * 1024)
                if not block:
                    break
                self.bytes += len(block)
                if self.bytes > MAX_BYTES:
                    raise WorkspaceError(f"revision is larger than {MAX_BYTES // (1024 * 1024)} MB")
                digest.update(block)
                out.write(block)
        os.chmod(target, 0o755 if executable else 0o644)
        self.entries.append((relpath, digest.hexdigest()))

    def fingerprint(self) -> str:
        digest = hashlib.sha256()
        for relpath, file_hash in sorted(self.entries):
            digest.update(f"{relpath}\0{file_hash}\n".encode())
        return digest.hexdigest()


def _copy_tree_files(source: Path, relpaths: list[str], copier: _Copier) -> None:
    source = source.resolve()
    for rel in relpaths:
        rel = PurePosixPath(rel).as_posix()
        if is_excluded_path(rel):
            continue
        path = source / rel
        # Symlinks are skipped: one pointing outside the tree would otherwise
        # smuggle host files into the build context.
        if path.is_symlink() or not path.is_file():
            continue
        with open(path, "rb") as stream:
            copier.add(rel, stream, executable=os.access(path, os.X_OK))


def _walk_relpaths(source: Path) -> list[str]:
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(source):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIRS and not d.endswith(".egg-info")]
        rel_dir = Path(dirpath).relative_to(source)
        for name in filenames:
            out.append((rel_dir / name).as_posix())
    return out


def _git_worktree_relpaths(target: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(target), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        capture_output=True, timeout=GIT_TIMEOUT, check=False,
    )
    if result.returncode != 0:
        raise WorkspaceError(f"git ls-files failed: {result.stderr.decode(errors='replace')[:300]}")
    return [p for p in result.stdout.decode("utf-8", errors="replace").split("\0") if p]


class _ExactReader:
    """File-like view of exactly ``size`` bytes of a shared stream."""

    def __init__(self, stream, size: int):
        self.stream = stream
        self.remaining = size

    def read(self, n: int = -1) -> bytes:
        if self.remaining <= 0:
            return b""
        n = self.remaining if n < 0 else min(n, self.remaining)
        data = self.stream.read(n)
        if not data:
            raise WorkspaceError("git cat-file ended early")
        self.remaining -= len(data)
        return data


def _extract_git_revision(target: Path, sha: str, prefix: str, copier: _Copier) -> None:
    """Copy a commit's tree verbatim.

    ``git archive`` is deliberately avoided: it honors export-ignore and
    export-subst attributes, and some repositories export-ignore their tests,
    which would silently drop the suite the regression test belongs to.
    """
    treeish = f"{sha}:{prefix}" if prefix else sha
    # --full-tree: run from a subdirectory, ls-tree would otherwise filter the
    # listing by that directory and return nothing for the subtree.
    listing = subprocess.run(
        ["git", "-C", str(target), "ls-tree", "-r", "-z", "--full-tree", treeish],
        capture_output=True, timeout=GIT_TIMEOUT, check=False,
    )
    if listing.returncode != 0:
        raise WorkspaceError(
            f"git ls-tree {treeish} failed: {listing.stderr.decode(errors='replace')[:300]}"
        )
    entries: list[tuple[str, str, bool]] = []
    for record in listing.stdout.split(b"\0"):
        if not record:
            continue
        meta, _, raw_path = record.partition(b"\t")
        try:
            mode, obj_type, obj = meta.decode().split()
        except ValueError:
            continue
        # Regular blobs only: symlinks (120000) and submodules (160000) are
        # never materialized, and absolute or `..` paths are rejected.
        if obj_type != "blob" or mode not in ("100644", "100755"):
            continue
        path = PurePosixPath(raw_path.decode("utf-8", errors="replace"))
        if path.is_absolute() or ".." in path.parts or is_excluded_path(path.as_posix()):
            continue
        entries.append((path.as_posix(), obj, mode == "100755"))

    proc = subprocess.Popen(
        ["git", "-C", str(target), "cat-file", "--batch"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    stdin, stdout = proc.stdin, proc.stdout
    assert stdin is not None and stdout is not None
    try:
        for rel, obj, executable in entries:
            stdin.write(f"{obj}\n".encode())
            stdin.flush()
            header = stdout.readline().decode(errors="replace").split()
            if len(header) != 3 or header[1] != "blob":
                raise WorkspaceError(f"could not read {rel} from {sha[:12]}")
            copier.add(rel, _ExactReader(stdout, int(header[2])), executable=executable)
            stdout.read(1)  # newline terminating each object
    finally:
        try:
            stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def materialize(target_dir: Path, spec: RevisionSpec, dest: Path) -> MaterializedRevision:
    """Copy one revision of ``target_dir`` into the empty directory ``dest``."""
    target_dir = Path(target_dir).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    copier = _Copier(dest)
    try:
        if spec.kind == "git":
            info = git_info(target_dir)
            if not info.get("is_repo"):
                raise WorkspaceError(f"{target_dir} is not a git repository, so `{spec.value}` cannot be resolved")
            sha = resolve_commit(target_dir, spec.value)
            _extract_git_revision(target_dir, sha, info.get("prefix", ""), copier)
            resolved = sha
        elif spec.kind == "worktree":
            info = git_info(target_dir)
            if info.get("is_repo"):
                relpaths = _git_worktree_relpaths(target_dir)
                resolved = f"working tree @ {info.get('head', '')[:12]}" + (" (modified)" if info.get("dirty") else "")
            else:
                relpaths = _walk_relpaths(target_dir)
                resolved = "working tree"
            _copy_tree_files(target_dir, relpaths, copier)
        elif spec.kind == "directory":
            source = Path(spec.value)
            if not source.is_dir():
                raise WorkspaceError(f"directory not found: {source}")
            _copy_tree_files(source, _walk_relpaths(source), copier)
            resolved = str(source)
        else:
            raise WorkspaceError(f"unknown revision kind: {spec.kind}")
    except subprocess.TimeoutExpired as exc:
        raise WorkspaceError(f"timed out materializing {spec.describe()}") from exc
    except OSError as exc:
        raise WorkspaceError(f"could not materialize {spec.describe()}: {exc}") from exc
    if copier.count == 0:
        raise WorkspaceError(f"{spec.describe()} contains no files")
    return MaterializedRevision(
        spec=spec, root=dest.resolve(), resolved=resolved,
        fingerprint=copier.fingerprint(), file_count=copier.count,
    )


def new_file_patch(relpath: str, content: str) -> str:
    """A git-style patch that adds one new file (apply with ``git apply``)."""
    if not content.endswith("\n"):
        content += "\n"
    lines = content.splitlines(keepends=True)
    header = (
        f"diff --git a/{relpath} b/{relpath}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{relpath}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n"
    )
    return header + "".join(f"+{line}" for line in lines)


def write_private_file(path: Path, content: str) -> None:
    """Write an artifact readable only by the current user."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    path.write_text(content, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def remove_tree(path: Optional[Path]) -> None:
    if path is not None:
        shutil.rmtree(path, ignore_errors=True)
