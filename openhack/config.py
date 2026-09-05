import json
import os
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

CONFIG_DIR = Path.home() / ".openhack"
CONFIG_PATH = CONFIG_DIR / "config"

_PROVIDER_KEY_FIELDS = {
    "openhack": "openhack_api_key",
}

_REMOVED_PROVIDER_IDS = frozenset({"opencode", "opencode-go"})


def _normalize_user_config(data: dict) -> dict:
    """Return a usable config when a previously selected provider was removed."""
    if data.get("provider") not in _REMOVED_PROVIDER_IDS:
        return data
    normalized = dict(data)
    normalized.update({"provider": "openhack", "model": "glm-5.3-flash"})
    return normalized


def _dotenv_nonempty_keys(path: Path) -> set[str]:
    """Return uppercase keys with non-empty values from a dotenv file."""
    keys: set[str] = set()
    if not path.exists():
        return keys
    try:
        for raw_line in path.read_text().splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            if "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and val != "":
                keys.add(key.upper())
    except OSError:
        return set()
    return keys


def load_user_config() -> dict:
    """Load persistent config from ~/.openhack/config."""
    if CONFIG_PATH.exists():
        try:
            loaded = json.loads(CONFIG_PATH.read_text())
            return _normalize_user_config(loaded) if isinstance(loaded, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_user_config(data: dict) -> None:
    """Save persistent config to ~/.openhack/config."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(CONFIG_DIR, 0o700)
    except OSError:
        pass
    existing = load_user_config()
    existing.update(data)
    CONFIG_PATH.write_text(json.dumps(existing, indent=2) + "\n")
    # Config now holds long-lived bearer tokens; restrict to owner-only read/write.
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


def resolve_provider(name: str) -> str:
    """Normalize provider name."""
    return "openhack" if name in _REMOVED_PROVIDER_IDS else name


PROD_APP_URL = "https://app.openhack.com"
PROD_BASE_URL = "https://api.openhack.com/v1"
DEV_APP_URL = "http://localhost:9080"
DEV_BASE_URL = "http://localhost:8787/v1"


class Settings(BaseSettings):
    """Minimal settings for the standalone scanner."""

    # Set OPENHACK_DEV=1 to point both URLs at local dev (Next.js app on :9080,
    # wrangler dev inference on :8787) instead of production.
    openhack_dev: bool = False

    llm_provider: str = "openhack"

    openhack_api_key: Optional[str] = None
    openhack_base_url: str = ""
    openhack_app_url: str = ""
    openhack_model_id: str = "glm-5.3-flash"
    # Throughput-first OpenRouter routing for hosted OpenHack inference.
    fast_mode: bool = False
    # Rotating contextual guidance on the TUI landing screen.
    tips_enabled: bool = True

    openhack_org_id: Optional[str] = None
    openhack_org_slug: Optional[str] = None
    openhack_org_name: Optional[str] = None
    openhack_user_email: Optional[str] = None
    openhack_user_first_name: Optional[str] = None
    openhack_user_last_name: Optional[str] = None
    # Read timeout is per socket read, and every call streams — so this is
    # "how long we tolerate SILENCE between chunks", not how long a generation
    # may take. A long answer keeps the timer resetting. At the old 600s a hung
    # upstream froze the TUI for ten minutes with no output before it even
    # errored (session 265af3d8: a 160s stall ending in an APIError), and with
    # retries the worst case was over an hour. 120s still allows a very slow
    # time-to-first-token while catching a dead connection quickly.
    openhack_read_timeout: int = 120
    openhack_connect_timeout: int = 30
    openhack_max_retries: int = 5

    # ...and because that timeout only sees BYTES, it misses an upstream that
    # wedges while still sending SSE keepalives — proven: a server emitting
    # `: keep-alive` every second never trips a 5s read timeout. So this second
    # limit measures decodable PROGRESS (content / reasoning / tool arguments /
    # usage) and abandons the stream when there's been none. max_tokens is 8192
    # and glm-5.2 streams ~160 tok/s, so a legitimate call tops out near 60s of
    # generation; 90s clears that with room to spare. Session cfeb868f hung 274s.
    openhack_stream_stall_timeout: int = 90

    # Send prompt_cache_key with API calls. Supported by OpenHack and OpenAI;
    # some OpenAI-compatible endpoints (e.g. Groq) reject unknown params.
    prompt_caching: bool = True

    recon_model_id: Optional[str] = None
    hunter_model_id: Optional[str] = None
    validator_model_id: Optional[str] = None
    browser_verifier_model_id: Optional[str] = None

    max_concurrent_hunters: int = 3
    max_concurrent_validators: int = 5

    compaction_threshold: float = 0.70
    tool_result_max_lines: int = 200
    checkpoint_enabled: bool = True

    # Agentic loop governor. There is no iteration cutoff: a run ends when the
    # agent calls finish_task, the user cancels, a real error occurs, or the
    # progress-aware stop fires (N consecutive turns with no *novel signal*).
    # Counting turns was the wrong governor — it killed productive runs mid-work
    # (a successful write_file on turn 60) while doing nothing about near-dup
    # thrash, which is what the novelty-keyed stale stop actually catches.
    # 0 = unlimited. Set a positive value only to opt into a hard backstop.
    agent_max_iterations: int = 0
    agent_stale_turn_limit: int = 8

    # Scan scoping — exclude paths that are never production web attack surface
    scan_exclude_patterns: list[str] = [
        "**/test/**", "**/tests/**", "**/__tests__/**", "**/spec/**",
        "**/__mocks__/**", "**/fixtures/**", "**/__fixtures__/**",
        "**/e2e/**", "**/cypress/**", "**/playwright/**",
        "**/cli/**", "**/CLI/**",
        "**/docs/**", "**/documentation/**",
        "**/examples/**", "**/example/**", "**/samples/**", "**/demo/**", "**/demos/**",
        "**/tutorial/**", "**/tutorials/**", "**/playground/**", "**/sandbox/**",
        "**/mock/**", "**/mocks/**", "**/stub/**", "**/stubs/**",
        "**/scripts/**", "**/tools/**", "**/devtools/**",
        "**/benchmarks/**", "**/benchmark/**",
        "**/integration-tests/**",
        "**/*.test.*", "**/*.spec.*", "**/test_*",
        "**/conftest.py", "**/jest.config.*", "**/vitest.config.*",
        "**/.storybook/**", "**/stories/**",
    ]

    # Feature deep dive
    feature_hunt_enabled: bool = True
    max_feature_hunters: int = 7
    feature_hunter_max_iterations: int = 75
    max_concurrent_feature_hunters: int = 2
    feature_hunter_model_id: Optional[str] = None

    # Sandbox verification
    sandbox_enabled: bool = False
    sandbox_max_exploit_attempts: int = 7
    sandbox_health_check_timeout: int = 120
    sandbox_health_check_path: str = "/"
    sandbox_teardown_on_complete: bool = True

    # Browser verification
    # Browser verification
    browser_verification_enabled: bool = False
    browser_headless: bool = True
    browser_max_exploit_attempts: int = 7
    browser_timeout_ms: int = 30000

    # Negotiate the Kitty keyboard protocol (CSI-u) in the TUI so modifier
    # combos that legacy terminal encoding collapses — Option/Alt+Backspace,
    # lone Escape, Ctrl+key — arrive disambiguated. Harmless on terminals that
    # don't support it (they ignore the request). Set false to force legacy.
    kitty_keyboard_protocol: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        # Ignore unrelated keys in a CWD .env / environment. The scanner runs
        # inside arbitrary target repos whose .env files contain keys we don't
        # own; without this, pydantic-settings' default extra="forbid" crashes
        # the CLI on any unknown key (e.g. a target's gemini_sandbox_proxy_command).
        extra="ignore",
    )

    def model_post_init(self, __context) -> None:
        if not self.openhack_app_url:
            self.openhack_app_url = DEV_APP_URL if self.openhack_dev else PROD_APP_URL
        if not self.openhack_base_url:
            self.openhack_base_url = DEV_BASE_URL if self.openhack_dev else PROD_BASE_URL


def _build_settings() -> Settings:
    """Build Settings, overlaying ~/.openhack/config values as env-like overrides."""
    user_cfg = load_user_config()
    env_overrides = {}
    for key, val in user_cfg.items():
        if val is not None and val != "":
            env_overrides[key.upper()] = str(val)

    dotenv_keys = _dotenv_nonempty_keys(Path(".env"))
    old_env = {}
    for k, v in env_overrides.items():
        # Respect explicit non-empty environment variables, but allow persisted
        # config to fill missing or blank values. Also let .env values win.
        current = os.environ.get(k)
        if (current is None or current == "") and k not in dotenv_keys:
            old_env[k] = current
            os.environ[k] = v

    try:
        s = Settings()
    finally:
        for k, prev in old_env.items():
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev
    return s


settings = _build_settings()


def reload_settings() -> None:
    """Reload settings in place so existing module imports see new values."""
    fresh = _build_settings()
    for field_name in Settings.model_fields:
        setattr(settings, field_name, getattr(fresh, field_name))
