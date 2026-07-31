"""Provider and model discovery modeled after OpenCode's Models.dev registry.

The scanner keeps a small, tested offline catalog for its first-party provider,
OpenCode Zen, and OpenCode Go.  When network access is available, Models.dev is
the source of truth for the much larger provider/model catalog.
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
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

MODELS_DEV_URL = "https://models.dev/api.json"
CATALOG_CACHE_PATH = Path.home() / ".openhack" / "models-dev.json"
CATALOG_CACHE_TTL = 24 * 60 * 60
_MEMORY_CATALOG: Optional[dict[str, Any]] = None
_MEMORY_CACHE_PATH: Optional[Path] = None
_MEMORY_LOCK = threading.Lock()

# Preserve the product's existing ranking.  These models always float to the
# top when a provider offers them.
PREFERRED_MODEL_IDS = (
    "grok-4.5",
    "glm-5.2",
    "kimi-k2.5",
    "gemma-4-31b",
)

OPENHACK_MODELS = (
    ("grok-4.5", "Grok 4.5", "Frontier model by xAI · strongest exploitation"),
    ("glm-5.2", "GLM 5.2", "Fast, capable long-horizon reasoning by Z.ai"),
    ("kimi-k2.5", "Kimi K2.5", "Multimodal security analysis by Moonshot AI"),
    ("gemma-4-31b", "Gemma 4 31B", "Open-weight model by Google"),
)

# Snapshot from Models.dev / OpenCode on 2026-07-30.  Live discovery replaces
# this whenever possible; it exists so /model remains useful offline.
ZEN_MODELS = (
    ("gpt-5.1-codex-mini", "GPT-5.1 Codex Mini"),
    ("claude-sonnet-4-6", "Claude Sonnet 4.6"),
    ("gpt-5.5-pro", "GPT-5.5 Pro"),
    ("glm-5", "GLM-5"),
    ("gemini-3.5-flash", "Gemini 3.5 Flash"),
    ("gpt-5.3-codex-spark", "GPT-5.3 Codex Spark"),
    ("claude-haiku-4-5", "Claude Haiku 4.5"),
    ("gpt-5.4-pro", "GPT-5.4 Pro"),
    ("qwen3.5-plus", "Qwen3.5 Plus"),
    ("gpt-5.6-sol", "GPT-5.6 Sol"),
    ("glm-5.1", "GLM-5.1"),
    ("gemini-3.5-flash-lite", "Gemini 3.5 Flash Lite"),
    ("claude-opus-4-6", "Claude Opus 4.6"),
    ("claude-fable-5", "Claude Fable 5"),
    ("gemini-3.1-pro", "Gemini 3.1 Pro Preview"),
    ("deepseek-v4-flash", "DeepSeek V4 Flash"),
    ("kimi-k2.5", "Kimi K2.5"),
    ("minimax-m2.7", "MiniMax M2.7"),
    ("claude-opus-4-8", "Claude Opus 4.8"),
    ("north-mini-code-free", "North Mini Code Free"),
    ("glm-5.2", "GLM-5.2"),
    ("ling-3.0-flash-free", "Ling 3.0 Flash Free"),
    ("laguna-s-2.1-free", "Laguna S 2.1 Free"),
    ("deepseek-v4-flash-free", "DeepSeek V4 Flash Free"),
    ("gemini-3-flash", "Gemini 3 Flash"),
    ("kimi-k2.6", "Kimi K2.6"),
    ("gpt-5.5", "GPT-5.5"),
    ("claude-opus-4-1", "Claude Opus 4.1"),
    ("minimax-m3", "MiniMax M3"),
    ("gpt-5", "GPT-5"),
    ("gpt-5-codex", "GPT-5 Codex"),
    ("claude-sonnet-4-5", "Claude Sonnet 4.5"),
    ("deepseek-v4-pro", "DeepSeek V4 Pro"),
    ("gpt-5.4", "GPT-5.4"),
    ("gpt-5.2-codex", "GPT-5.2 Codex"),
    ("gpt-5.4-nano", "GPT-5.4 Nano"),
    ("gemini-3.6-flash", "Gemini 3.6 Flash"),
    ("claude-sonnet-4", "Claude Sonnet 4"),
    ("gpt-5.4-mini", "GPT-5.4 Mini"),
    ("minimax-m2.5", "MiniMax M2.5"),
    ("gpt-5.6-luna", "GPT-5.6 Luna"),
    ("mimo-v2.5-free", "MiMo V2.5 Free"),
    ("gpt-5.2", "GPT-5.2"),
    ("gpt-5.3-codex", "GPT-5.3 Codex"),
    ("grok-4.5", "Grok 4.5"),
    ("kimi-k2.7-code", "Kimi K2.7 Code"),
    ("gpt-5.1", "GPT-5.1"),
    ("big-pickle", "Big Pickle"),
    ("grok-build-0.1", "Grok Build 0.1"),
    ("kimi-k3", "Kimi K3"),
    ("gpt-5.1-codex", "GPT-5.1 Codex"),
    ("gpt-5-nano", "GPT-5 Nano"),
    ("claude-opus-4-7", "Claude Opus 4.7"),
    ("gpt-5.6-terra", "GPT-5.6 Terra"),
    ("claude-opus-4-5", "Claude Opus 4.5"),
    ("claude-sonnet-5", "Claude Sonnet 5"),
    ("nemotron-3-ultra-free", "Nemotron 3 Ultra Free"),
    ("claude-opus-5", "Claude Opus 5"),
    ("gpt-5.1-codex-max", "GPT-5.1 Codex Max"),
    ("qwen3.6-plus", "Qwen3.6 Plus"),
)

GO_MODELS = (
    ("qwen3.7-plus", "Qwen3.7 Plus"),
    ("glm-5.1", "GLM-5.1"),
    ("deepseek-v4-flash", "DeepSeek V4 Flash"),
    ("minimax-m2.7", "MiniMax M2.7"),
    ("glm-5.2", "GLM-5.2"),
    ("qwen3.7-max", "Qwen3.7 Max"),
    ("kimi-k2.6", "Kimi K2.6"),
    ("minimax-m3", "MiniMax M3"),
    ("hy3", "Hy3"),
    ("deepseek-v4-pro", "DeepSeek V4 Pro"),
    ("mimo-v2.5", "MiMo V2.5"),
    ("grok-4.5", "Grok 4.5"),
    ("kimi-k2.7-code", "Kimi K2.7 Code"),
    ("kimi-k3", "Kimi K3 (2x usage)"),
    ("mimo-v2.5-pro", "MiMo V2.5 Pro"),
    ("qwen3.6-plus", "Qwen3.6 Plus"),
)


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

    Network failure is deliberately non-fatal: callers always have the bundled
    catalog for first-party, Zen, Go, and common BYOK providers.
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


def provider_from_models_dev(
    provider_id: str,
    *,
    catalog: Optional[dict[str, Any]] = None,
) -> Optional[CatalogProvider]:
    catalog = catalog or load_models_dev(allow_network=False)
    raw = catalog.get(provider_id) if catalog else None
    if not isinstance(raw, dict):
        return None
    api = _expand_api(str(raw.get("api") or ""))
    env = tuple(str(value) for value in raw.get("env") or [] if value)
    models_raw = raw.get("models") or {}
    if not api or not isinstance(models_raw, dict):
        return None
    models = tuple(
        {
            "id": str(model_id),
            "label": str(info.get("name") or model_id),
            "desc": str(info.get("description") or ""),
        }
        for model_id, info in models_raw.items()
        if isinstance(info, dict) and info.get("status") != "deprecated"
    )
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
        if not isinstance(raw, dict):
            continue
        if raw.get("npm") not in ("@ai-sdk/openai-compatible", "@ai-sdk/openai"):
            continue
        provider = provider_from_models_dev(provider_id, catalog=catalog)
        if provider and provider.models and len(provider.env) <= 1:
            found.append(provider)
    return sorted(found, key=lambda item: item.name.casefold())


def rank_model_ids(ids: Iterable[str]) -> list[str]:
    """Deduplicate model IDs while keeping OpenHack's established top ranking."""
    unique = list(dict.fromkeys(str(model_id) for model_id in ids if model_id))
    rank = {model_id: index for index, model_id in enumerate(PREFERRED_MODEL_IDS)}
    original = {model_id: index for index, model_id in enumerate(unique)}
    return sorted(
        unique,
        key=lambda model_id: (
            0 if model_id in rank else 1,
            rank.get(model_id, original[model_id]),
        ),
    )


def bundled_models(provider_id: str) -> list[dict[str, str]]:
    if provider_id == "openhack":
        return [
            {"id": model_id, "label": label, "desc": desc}
            for model_id, label, desc in OPENHACK_MODELS
        ]
    if provider_id == "opencode":
        rows = ZEN_MODELS
    elif provider_id == "opencode-go":
        rows = GO_MODELS
    else:
        remote = provider_from_models_dev(provider_id)
        return list(remote.models) if remote else []

    by_id = {
        model_id: {"id": model_id, "label": label, "desc": ""}
        for model_id, label in rows
    }
    return [by_id[model_id] for model_id in rank_model_ids(by_id)]


def merge_models(
    provider_id: str,
    live_ids: Optional[Iterable[str]] = None,
) -> list[dict[str, str]]:
    """Merge live model IDs with catalog metadata and rank the result."""
    bundled = bundled_models(provider_id)
    metadata = {entry["id"]: entry for entry in bundled}
    ids = list(live_ids or []) or list(metadata)
    if not ids:
        return []
    return [
        metadata.get(model_id, {"id": model_id, "label": model_id, "desc": ""})
        for model_id in rank_model_ids(ids)
    ]
