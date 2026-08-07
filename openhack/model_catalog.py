"""Models.dev-backed provider and model discovery.

The scanner keeps a small, tested offline catalog for its first-party provider.
When network access is available, Models.dev supplies the larger provider/model
catalog.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

logger = logging.getLogger(__name__)

MODELS_DEV_URL = "https://models.dev/api.json"
CATALOG_CACHE_PATH = Path.home() / ".openhack" / "models-dev.json"
CATALOG_CACHE_TTL = 24 * 60 * 60
_MEMORY_CATALOG: Optional[dict[str, Any]] = None
_MEMORY_CACHE_PATH: Optional[Path] = None
_MEMORY_LOCK = threading.Lock()

# Hosted OpenHack models deliberately are not bundled in the CLI. The deployed
# inference service owns that catalog and returns its metadata from /v1/models.
# This prevents a newer terminal from advertising routes that have not actually
# been deployed to inference yet.

# These third-party plans are intentionally not OpenHack providers. Keep the
# exclusion at the catalog boundary so cached or refreshed Models.dev data
# cannot silently add them back.
EXCLUDED_PROVIDER_IDS = frozenset({"opencode", "opencode-go"})

@dataclass(frozen=True)
class CatalogProvider:
    id: str
    name: str
    api: str
    env: tuple[str, ...]
    models: tuple[dict[str, str], ...]
    npm: str = ""


def _read_cache(path: Path = CATALOG_CACHE_PATH) -> Optional[dict[str, Any]]:
    try:
        raw = json.loads(path.read_text())
        return raw if isinstance(raw, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(data: dict[str, Any], path: Path = CATALOG_CACHE_PATH) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, separators=(",", ":")))
        os.chmod(tmp, 0o600)
        tmp.replace(path)
    except OSError as exc:
        logger.debug("could not cache Models.dev catalog: %s", exc)


def load_models_dev(
    *,
    refresh: bool = False,
    allow_network: bool = True,
    timeout: float = 4.0,
    cache_path: Path = CATALOG_CACHE_PATH,
) -> Optional[dict[str, Any]]:
    """Return the Models.dev registry, using a one-day local cache.

    Network failure is deliberately non-fatal: callers retain the bundled
    first-party catalog and any previously cached provider data.
    """
    global _MEMORY_CATALOG, _MEMORY_CACHE_PATH
    with _MEMORY_LOCK:
        memory = (
            _MEMORY_CATALOG
            if _MEMORY_CACHE_PATH == cache_path
            else None
        )
    if memory is not None and not refresh:
        return memory

    cached = memory or _read_cache(cache_path)
    if cached is not None:
        with _MEMORY_LOCK:
            _MEMORY_CATALOG = cached
            _MEMORY_CACHE_PATH = cache_path
    fresh = False
    try:
        fresh = time.time() - cache_path.stat().st_mtime < CATALOG_CACHE_TTL
    except OSError:
        pass
    if cached and fresh and not refresh:
        return cached
    if not allow_network:
        return cached

    try:
        req = urllib.request.Request(
            MODELS_DEV_URL,
            headers={"Accept": "application/json", "User-Agent": "openhack"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        if isinstance(data, dict):
            with _MEMORY_LOCK:
                _MEMORY_CATALOG = data
                _MEMORY_CACHE_PATH = cache_path
            _write_cache(data, cache_path)
            return data
    except Exception as exc:
        logger.debug("Models.dev discovery failed: %s", exc)
    return cached


def _expand_api(api: str) -> Optional[str]:
    """Expand ${ENV} placeholders or reject an unresolved endpoint."""
    expanded = os.path.expandvars(api)
    return None if "${" in expanded else expanded.rstrip("/")


def models_from_models_dev(
    provider_id: str,
    *,
    catalog: Optional[dict[str, Any]] = None,
) -> list[dict[str, str]]:
    """Return model metadata without requiring Models.dev to define an API URL.

    Curated providers already have endpoints in ``providers.py``. Models.dev
    sometimes omits ``api`` for those providers while still publishing their
    complete model catalogs.
    """
    if provider_id in EXCLUDED_PROVIDER_IDS:
        return []
    catalog = catalog or load_models_dev(allow_network=False)
    raw = catalog.get(provider_id) if catalog else None
    if not isinstance(raw, dict):
        return []
    models_raw = raw.get("models") or {}
    if not isinstance(models_raw, dict):
        return []
    return [
        {
            "id": str(model_id),
            "label": str(info.get("name") or model_id),
            "desc": str(info.get("description") or ""),
        }
        for model_id, info in models_raw.items()
        if isinstance(info, dict) and info.get("status") != "deprecated"
    ]


def provider_from_models_dev(
    provider_id: str,
    *,
    catalog: Optional[dict[str, Any]] = None,
) -> Optional[CatalogProvider]:
    if provider_id in EXCLUDED_PROVIDER_IDS:
        return None
    catalog = catalog or load_models_dev(allow_network=False)
    raw = catalog.get(provider_id) if catalog else None
    if not isinstance(raw, dict):
        return None
    api = _expand_api(str(raw.get("api") or ""))
    env = tuple(str(value) for value in raw.get("env") or [] if value)
    if not api:
        return None
    models = tuple(models_from_models_dev(provider_id, catalog=catalog))
    return CatalogProvider(
        id=provider_id,
        name=str(raw.get("name") or provider_id),
        api=api,
        env=env,
        models=models,
        npm=str(raw.get("npm") or ""),
    )


def discover_compatible_providers(
    *, refresh: bool = False, allow_network: bool = False
) -> list[CatalogProvider]:
    """Discover providers that explicitly use OpenAI-compatible wire format."""
    catalog = load_models_dev(refresh=refresh, allow_network=allow_network)
    if not catalog:
        return []
    found: list[CatalogProvider] = []
    for provider_id, raw in catalog.items():
        if provider_id in EXCLUDED_PROVIDER_IDS:
            continue
        if not isinstance(raw, dict):
            continue
        if raw.get("npm") not in ("@ai-sdk/openai-compatible", "@ai-sdk/openai"):
            continue
        provider = provider_from_models_dev(provider_id, catalog=catalog)
        if provider and provider.models and len(provider.env) <= 1:
            found.append(provider)
    return sorted(found, key=lambda item: item.name.casefold())


def rank_model_ids(ids: Iterable[str]) -> list[str]:
    """Deduplicate IDs while preserving the provider's authoritative order."""
    return list(dict.fromkeys(str(model_id) for model_id in ids if model_id))


def bundled_models(provider_id: str) -> list[dict[str, str]]:
    if provider_id == "openhack":
        return []
    return models_from_models_dev(provider_id)


def merge_models(
    provider_id: str,
    live_models: Optional[Iterable[str | Mapping[str, Any]]] = None,
) -> list[dict[str, str]]:
    """Merge provider results without inventing hosted OpenHack entries."""
    bundled = bundled_models(provider_id)
    metadata = {entry["id"]: entry for entry in bundled}
    raw_models = list(live_models or [])
    if not raw_models:
        return bundled

    merged: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in raw_models:
        if isinstance(raw, Mapping):
            model_id = str(raw.get("id") or "")
            live = {
                str(key): str(value)
                for key, value in raw.items()
                if value is not None
            }
        else:
            model_id = str(raw)
            live = {"id": model_id}
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        base = metadata.get(
            model_id,
            {"id": model_id, "label": model_id, "desc": ""},
        )
        merged.append({**base, **live, "id": model_id})
    if not merged:
        return []
    return merged
