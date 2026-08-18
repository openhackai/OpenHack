"""
Startup update check + announcements fetcher.

Calls GET /updates on the inference worker. Never blocks the TUI or
raises to the user on failure — network errors are silently swallowed.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

from openhack import __version__
from openhack.config import CONFIG_DIR

logger = logging.getLogger(__name__)

_DISMISSED_FILE = CONFIG_DIR / "dismissed_announcements.json"
_LAST_CHECK_FILE = CONFIG_DIR / ".last_update_check"
_SKIPPED_UPDATE_FILE = CONFIG_DIR / ".skipped_update"
_UPDATE_CACHE_FILE = CONFIG_DIR / ".update_manifest.json"
_RECHECK_INTERVAL = 3600  # 1 hour


@dataclass
class LatestRelease:
    version: str
    published_at: str = ""
    download_url: str = ""
    release_notes: str = ""


@dataclass
class Announcement:
    id: str
    level: str  # "info" | "warning" | "critical"
    title: str
    body: str = ""
    placement: list[str] = field(default_factory=list)
    published_at: str = ""
    expires_at: Optional[str] = None


@dataclass
class UpdateInfo:
    latest: Optional[LatestRelease] = None
    announcements: list[Announcement] = field(default_factory=list)
    has_update: bool = False


@dataclass
class InstallResult:
    success: bool
    method: str
    command: list[str] = field(default_factory=list)
    output: str = ""
    error: str = ""


def _get_updates_url() -> str:
    if os.environ.get("OPENHACK_DEV", "0") == "1":
        return "http://localhost:8787/updates"
    return "https://api.openhack.com/updates"


def _semver_gt(a: str, b: str) -> bool:
    """Return True if version `a` is strictly greater than `b` (semver major.minor.patch)."""
    def _parse(v: str) -> tuple[int, ...]:
        v = v.lstrip("v")
        parts = v.split("-")[0].split("+")[0]  # strip pre-release/build
        return tuple(int(x) for x in parts.split(".") if x.isdigit())
    try:
        return _parse(a) > _parse(b)
    except (ValueError, TypeError):
        return False


def _is_expired(expires_at: Optional[str]) -> bool:
    if not expires_at:
        return False
    try:
        exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        return datetime.now(timezone.utc) > exp
    except (ValueError, TypeError):
        return False


def _load_dismissed() -> set[str]:
    try:
        data = json.loads(_DISMISSED_FILE.read_text())
        return set(data) if isinstance(data, list) else set()
    except Exception:
        return set()


def save_dismissed(ann_id: str) -> None:
    """Persist an announcement ID as dismissed so it won't re-appear."""
    dismissed = _load_dismissed()
    dismissed.add(ann_id)
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        _DISMISSED_FILE.write_text(json.dumps(sorted(dismissed)))
    except Exception:
        pass


def skipped_update_version() -> str:
    try:
        return _SKIPPED_UPDATE_FILE.read_text().strip()
    except Exception:
        return ""


def save_skipped_update(version: str) -> None:
    """Suppress this release until a strictly newer release is available."""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        _SKIPPED_UPDATE_FILE.write_text(version.strip())
    except Exception:
        pass


def is_update_skipped(version: str) -> bool:
    skipped = skipped_update_version()
    return bool(skipped and not _semver_gt(version, skipped))


def _is_source_checkout() -> bool:
    module = Path(__file__).resolve()
    return any(
        (parent / ".git").exists() and (parent / "pyproject.toml").exists()
        for parent in module.parents
    )


def detect_install_method() -> str:
    """Identify the package manager that owns the running OpenHack binary."""
    executable = str(Path(sys.executable).resolve()).lower()
    if "pipx" in executable and "openhack" in executable:
        return "pipx"
    if "uv/tools/openhack" in executable or "uv\\tools\\openhack" in executable:
        return "uv"
    if _is_source_checkout():
        return "development"
    # A normal venv/system install can safely upgrade itself through its own
    # interpreter. This avoids guessing based only on which global tools happen
    # to be installed on PATH.
    return "pip"


_VERSION_RE = re.compile(r"^v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


def build_update_command(version: str, method: Optional[str] = None) -> list[str]:
    """Build an argument-vector update command; never invoke a shell."""
    if not _VERSION_RE.fullmatch(version.strip()):
        raise ValueError(f"invalid update version: {version!r}")
    target = version.strip().lstrip("v")
    package = f"openhack=={target}"
    method = method or detect_install_method()
    if method == "pipx":
        executable = shutil.which("pipx") or "pipx"
        return [executable, "install", "--force", package]
    if method == "uv":
        executable = shutil.which("uv") or "uv"
        return [executable, "tool", "install", "--force", package]
    if method == "pip":
        return [sys.executable, "-m", "pip", "install", "--upgrade", package]
    if method == "development":
        raise RuntimeError(
            "OpenHack is running from a source checkout; update it with git instead"
        )
    raise RuntimeError(f"unsupported OpenHack installation method: {method}")


async def install_update(version: str, *, dry_run: bool = False) -> InstallResult:
    """Install one exact release, returning a UI-safe result instead of raising."""
    import asyncio

    method = "test" if dry_run else detect_install_method()
    if dry_run:
        return InstallResult(
            success=True,
            method=method,
            command=["dry-run", "install", f"openhack=={version.lstrip('v')}"]
        )
    try:
        command = build_update_command(version, method)
    except Exception as exc:
        return InstallResult(success=False, method=method, error=str(exc))

    def _run() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=180,
            check=False,
        )

    try:
        completed = await asyncio.to_thread(_run)
    except Exception as exc:
        return InstallResult(
            success=False, method=method, command=command, error=str(exc)
        )
    output = (completed.stdout or "").strip()
    error = (completed.stderr or "").strip()
    return InstallResult(
        success=completed.returncode == 0,
        method=method,
        command=command,
        output=output[-2000:],
        error=error[-2000:],
    )


def _should_check() -> bool:
    """Don't re-check if we already checked within this hour."""
    try:
        ts = float(_LAST_CHECK_FILE.read_text().strip())
        return (time.time() - ts) > _RECHECK_INTERVAL
    except Exception:
        return True


def _mark_checked() -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        _LAST_CHECK_FILE.write_text(str(time.time()))
    except Exception:
        pass


def _load_cached_manifest() -> Optional[dict]:
    try:
        data = json.loads(_UPDATE_CACHE_FILE.read_text())
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _save_cached_manifest(data: dict) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        _UPDATE_CACHE_FILE.write_text(json.dumps(data))
    except Exception:
        pass


def _parse_update_info(data: dict) -> UpdateInfo:
    info = UpdateInfo()

    latest_raw = data.get("latest")
    if latest_raw and isinstance(latest_raw, dict):
        info.latest = LatestRelease(
            version=latest_raw.get("version", ""),
            published_at=latest_raw.get("publishedAt", ""),
            download_url=latest_raw.get("downloadUrl", ""),
            release_notes=latest_raw.get("releaseNotes", ""),
        )
        if info.latest.version and _semver_gt(info.latest.version, __version__):
            info.has_update = True

    dismissed = _load_dismissed()
    for ann_raw in data.get("announcements") or []:
        if not isinstance(ann_raw, dict):
            continue
        ann_id = ann_raw.get("id", "")
        if ann_id in dismissed or _is_expired(ann_raw.get("expiresAt")):
            continue
        info.announcements.append(Announcement(
            id=ann_id,
            level=ann_raw.get("level", "info"),
            title=ann_raw.get("title", ""),
            body=ann_raw.get("body", ""),
            placement=ann_raw.get("placement") or [],
            published_at=ann_raw.get("publishedAt", ""),
            expires_at=ann_raw.get("expiresAt"),
        ))
    return info


async def fetch_updates(force: bool = False) -> Optional[UpdateInfo]:
    """Fetch update info from /updates. Returns None on any failure."""
    if not force and not _should_check():
        cached = _load_cached_manifest()
        return _parse_update_info(cached) if cached is not None else None

    url = _get_updates_url()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, params={"current": __version__})
            if resp.status_code != 200:
                return None
            data = resp.json()
            # Releases are distributed through PyPI. Keep the OpenHack API as
            # the source for notes and announcements, but fall back to PyPI's
            # canonical package version when the release manifest is empty.
            if not data.get("latest"):
                pypi = await client.get("https://pypi.org/pypi/openhack/json")
                if pypi.status_code == 200:
                    pypi_data = pypi.json()
                    version = (pypi_data.get("info") or {}).get("version", "")
                    if version:
                        data["latest"] = {
                            "version": version,
                            "downloadUrl": "https://pypi.org/project/openhack/",
                        }
    except Exception:
        return None

    _mark_checked()
    _save_cached_manifest(data)
    return _parse_update_info(data)
