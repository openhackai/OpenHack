"""
Interactive configuration wizard for OpenHack.

Two entry points:
  - run_first_time_setup()  — auto-launched when ~/.openhack/config is absent
  - run_setup_command()     — triggered by /setup inside the TUI (async)

Uses prompt_toolkit for arrow-key driven selection menus, secure password
input for API keys, and a final confirmation screen.
"""

import asyncio
import os
from typing import Optional

from prompt_toolkit import print_formatted_text
from prompt_toolkit.shortcuts import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.application import Application
from prompt_toolkit.layout.containers import HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.layout import Layout

from openhack.auth import (
    DeviceLoginCancelled,
    DeviceLoginError,
    DeviceLoginExpired,
    device_login,
)
from openhack.config import (
    CONFIG_PATH,
    load_user_config,
    save_user_config,
    resolve_provider,
    reload_settings,
    settings,
)

DIM = '<style fg="ansigray">'
EDIM = '</style>'
B = '<b>'
EB = '</b>'
CYAN = '<ansicyan>'
ECYAN = '</ansicyan>'
GREEN = '<ansigreen>'
EGREEN = '</ansigreen>'
YELLOW = '<ansiyellow>'
EYELLOW = '</ansiyellow>'


def _html(text: str) -> None:
    print_formatted_text(HTML(text))


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _clear() -> None:
    print("\033[2J\033[H", end="", flush=True)


# ── Provider / model definitions ──────────────────────────────────

PROVIDERS = [
    {
        "key": "openhack",
        "display": "OpenHack",
        "hint": "Recommended — no setup required, free tier available",
        "key_field": "openhack_api_key",
        "key_env": "OPENHACK_API_KEY",
        # key_url is built dynamically from settings.openhack_app_url at display time.
        "models": [
            ("glm-5.2", "GLM 5.2", "Recommended · best balance for agentic security work"),
            ("grok-4.5", "Grok 4.5", "Deep exploitation · difficult attack chains"),
            ("gemma-4-31b", "Gemma 4 31B", "Fast and open-weight"),
            ("kimi-k2.5", "Kimi K2.5", "Alternative · multimodal security analysis"),
            # mistral-large-2512 removed: no permitted inference provider
            # currently serves it, so it cannot be routed.
        ],
        "default_model": "glm-5.2",
    },
]


def _mask_key(key: str) -> str:
    if not key:
        return "(not set)"
    if len(key) <= 12:
        return key[:2] + "•" * (len(key) - 2)
    return key[:6] + "•" * 8 + key[-4:]

def _has_running_loop() -> bool:
    try:
        loop = asyncio.get_running_loop()
        return loop.is_running()
    except RuntimeError:
        return False


async def _input_async(message: str, is_password: bool = False) -> str:
    """Async text input with full editing keybindings (word jump/delete)."""
    session: PromptSession = PromptSession()
    return await session.prompt_async(message, is_password=is_password)


# ── Arrow-key selection menu ──────────────────────────────────────

async def _select_menu_async(title: str, items: list[tuple[str, str, str]], default_idx: int = 0) -> int:
    """Render an arrow-key driven selection menu. Returns the chosen index.

    items: list of (value, label, hint)
    """
    selected = [default_idx]

    def _get_text():
        lines = []
        lines.append(("class:title", f"  {title}\n\n"))
        for i, (_, label, hint) in enumerate(items):
            if i == selected[0]:
                lines.append(("class:selected", f"  ❯ {label}"))
                if hint:
                    lines.append(("class:hint.selected", f"  {hint}"))
                lines.append(("", "\n"))
            else:
                lines.append(("class:unselected", f"    {label}"))
                if hint:
                    lines.append(("class:hint", f"  {hint}"))
                lines.append(("", "\n"))
        lines.append(("class:footer", "\n  ↑/↓ to move · Enter to select · q to cancel"))
        return lines

    kb = KeyBindings()
    result = [None]

    @kb.add("up")
    @kb.add("k")
    def _up(event):
        selected[0] = (selected[0] - 1) % len(items)

    @kb.add("down")
    @kb.add("j")
    def _down(event):
        selected[0] = (selected[0] + 1) % len(items)

    @kb.add("enter")
    def _enter(event):
        result[0] = selected[0]
        event.app.exit()

    @kb.add("q")
    @kb.add("escape")
    def _quit(event):
        result[0] = -1
        event.app.exit()

    from prompt_toolkit.styles import Style
    style = Style.from_dict({
        "title": "bold",
        "selected": "bold ansibrightcyan",
        "hint.selected": "ansigray",
        "unselected": "",
        "hint": "ansigray",
        "footer": "ansigray italic",
    })

    control = FormattedTextControl(_get_text)
    window = Window(content=control, always_hide_cursor=True)
    layout = Layout(HSplit([window]))
    app = Application(layout=layout, key_bindings=kb, style=style, full_screen=False)
    await app.run_async()

    return result[0] if result[0] is not None else -1


def _select_menu(title: str, items: list[tuple[str, str, str]], default_idx: int = 0) -> int:
    """Sync wrapper — delegates to async impl."""
    if _has_running_loop():
        raise RuntimeError("Use _select_menu_async from within an event loop")

    return asyncio.run(_select_menu_async(title, items, default_idx))


# ── API key input ─────────────────────────────────────────────────

async def _prompt_api_key(provider: dict, existing_key: Optional[str] = None) -> Optional[str]:
    """Prompt for an API key with masked display."""
    _html("")
    _html(f'  {B}API Key for {_esc(provider["display"])}{EB}')
    key_url = f"{settings.openhack_app_url.rstrip('/')}/settings/api-keys"
    _html(f'  {DIM}Get your key at: {_esc(key_url)}{EDIM}')
    _html("")

    if existing_key:
        _html(f'  {DIM}Current: {_esc(_mask_key(existing_key))}{EDIM}')
        _html(f'  {DIM}Press Enter to keep existing key, or paste a new one{EDIM}')
        _html("")

    env_val = os.environ.get(provider["key_env"])
    if env_val:
        _html(f'  {DIM}Found in environment: ${_esc(provider["key_env"])} = {_esc(_mask_key(env_val))}{EDIM}')
        _html(f'  {DIM}Press Enter to use environment value{EDIM}')
        _html("")

    try:
        key = (await _input_async("  API Key: ", is_password=True)).strip()
    except (EOFError, KeyboardInterrupt):
        return existing_key

    if not key:
        if existing_key:
            return existing_key
        if env_val:
            return env_val
        return None

    return key


# ── Base URL input (for OpenHack provider) ───────────────────────────

async def _prompt_base_url(existing: Optional[str] = None) -> str:
    if not existing:
        existing = settings.openhack_base_url
    _html("")
    _html(f'  {B}OpenHack Base URL{EB}')
    _html(f'  {DIM}Default: {_esc(existing)}{EDIM}')
    _html(f'  {DIM}Press Enter to keep default{EDIM}')
    _html("")
    try:
        url = (await _input_async("  Base URL: ")).strip()
    except (EOFError, KeyboardInterrupt):
        return existing
    return url if url else existing


# ── Model selection ───────────────────────────────────────────────

async def _pick_model_async(
    provider: dict,
    api_key: Optional[str],
    base_url: Optional[str],
    default_model: str,
) -> str:
    """Let the user pick from the models the API actually serves.

    Fetches the live model list from GET /v1/models; falls back to the
    provider's hardcoded list if the call fails. Returns the chosen model id.
    """
    from openhack.agents.llm import fetch_available_models

    described = {mid: (label, desc) for mid, label, desc in provider["models"]}
    fetched = fetch_available_models(api_key=api_key, base_url=base_url)
    model_ids = fetched or [m[0] for m in provider["models"]]

    items: list[tuple[str, str, str]] = []
    for mid in model_ids:
        label, desc = described.get(mid, (mid, ""))
        items.append((mid, label, desc))
    if not items:
        return default_model

    default_idx = next((i for i, (mid, _, _) in enumerate(items) if mid == default_model), 0)
    idx = await _select_menu_async("Choose a model", items, default_idx=default_idx)
    if idx < 0:
        return default_model
    return items[idx][0]


# ── Summary / confirmation ────────────────────────────────────────

async def _show_summary(provider: dict, model_id: str, api_key: Optional[str], base_url: Optional[str] = None, org_name: Optional[str] = None) -> bool:
    _html("")
    _html(f'  {"━" * 50}')
    _html(f'  {B}Configuration Summary{EB}')
    _html(f'  {"━" * 50}')
    _html("")
    _html(f'  {B}Provider:{EB}  {_esc(provider["display"])}')
    if org_name:
        _html(f'  {B}Org:{EB}       {_esc(org_name)}')
    _html(f'  {B}Model:{EB}     {_esc(model_id)}')
    _html(f'  {B}API Key:{EB}   {_esc(_mask_key(api_key or ""))}')
    if base_url and provider["key"] == "openhack":
        _html(f'  {B}Base URL:{EB}  {_esc(base_url)}')
    _html("")
    _html(f'  {DIM}Config will be saved to {_esc(str(CONFIG_PATH))}{EDIM}')
    _html("")

    try:
        confirm = (await _input_async("  Save this configuration? [Y/n] ")).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False

    return confirm in ("", "y", "yes")


# ── First-time setup wizard ──────────────────────────────────────

def _banner() -> None:
    _html("")
    _html(f'  <b><ansibrightwhite>            ██</ansibrightwhite></b>')
    _html(f'  <b><ansibrightwhite>            ██</ansibrightwhite></b>')
    _html(f'  <b><ansibrightwhite>            ██</ansibrightwhite></b>')
    _html(f'  <b><ansibrightwhite>     ████████████████</ansibrightwhite></b>')
    _html("")
    _html(f'  <b><ansibrightwhite>       ████████████</ansibrightwhite></b>')
    _html("")
    _html(f'  <b><ansibrightwhite>         ████████</ansibrightwhite></b>')
    _html("")
    _html(f'  <b><ansicyan>  OpenHack</ansicyan></b>')
    _html("")
    _html(f'  {DIM}The open-source security agent.{EDIM}')
    _html("")


def _setup_banner() -> None:
    _html("")
    _html(f'  <b><ansibrightwhite>            ██</ansibrightwhite></b>')
    _html(f'  <b><ansibrightwhite>            ██</ansibrightwhite></b>')
    _html(f'  <b><ansibrightwhite>            ██</ansibrightwhite></b>')
    _html(f'  <b><ansibrightwhite>     ████████████████</ansibrightwhite></b>')
    _html("")
    _html(f'  <b><ansibrightwhite>       ████████████</ansibrightwhite></b>')
    _html("")
    _html(f'  <b><ansibrightwhite>         ████████</ansibrightwhite></b>')
    _html("")
    _html(f'  <b><ansicyan>  OpenHack</ansicyan></b> — Configuration')
    _html("")
    _html(f'  {DIM}Update your settings and API key.{EDIM}')
    _html("")


async def _run_first_time_onboarding() -> bool:
    """Short first-run path: connect, verify, choose a default, enter the TUI."""
    from openhack.agents.llm import fetch_available_models

    _banner()
    idx = await _select_menu_async(
        "Connect a provider",
        [
            ("openhack", "OpenHack", "Recommended · free credits on signup"),
            ("openai", "OpenAI", "API key or ChatGPT Plus/Pro"),
            ("anthropic", "Anthropic", "API key"),
            ("google", "Google AI Studio", "API key"),
            ("other", "Other…", "Browse every supported provider"),
        ],
    )
    if idx < 0:
        _html(f"  {DIM}Onboarding cancelled.{EDIM}")
        _html("")
        return False

    provider_id = ("openhack", "openai", "anthropic", "google", "other")[idx]
    if provider_id != "openhack":
        connected = await run_provider_connect(
            None if provider_id == "other" else provider_id
        )
        if connected:
            save_user_config({"onboarding_version": 1})
            _html(f"  {GREEN}✓{EGREEN} {B}Security agent ready.{EB}")
            _html(f"  {DIM}Opening OpenHack…{EDIM}")
            _html("")
        return connected

    provider = PROVIDERS[0]
    cfg = load_user_config()
    auth_idx = await _select_menu_async(
        "Connect OpenHack",
        [
            ("login", "Login with OpenHack", "Recommended · opens your browser"),
            ("apikey", "Use an API key", "Paste an existing OpenHack key"),
        ],
    )
    if auth_idx < 0:
        return False

    login_result = None
    api_key: Optional[str] = None
    if auth_idx == 0:
        try:
            login_result = await device_login(
                cfg.get("openhack_app_url") or settings.openhack_app_url
            )
            api_key = login_result.token
        except (DeviceLoginCancelled, DeviceLoginExpired, DeviceLoginError) as exc:
            _html(f"  {YELLOW}⚠{EYELLOW}  Connection failed: {_esc(str(exc))}")
            _html("")
            return False
    else:
        api_key = await _prompt_api_key(provider, cfg.get("openhack_api_key"))
        if not api_key:
            _html(f"  {YELLOW}⚠{EYELLOW}  An API key is required.")
            _html("")
            return False

    _html("")
    _html(f"  {DIM}Verifying OpenHack inference…{EDIM}")
    live_models = await asyncio.to_thread(
        fetch_available_models,
        api_key=api_key,
        base_url=settings.openhack_base_url,
        timeout=8,
    )
    if not live_models:
        _html(f"  {YELLOW}⚠{EYELLOW}  Could not verify this connection.")
        _html(f"  {DIM}Check the credential and try again.{EDIM}")
        _html("")
        return False
    _html(f"  {GREEN}✓{EGREEN} Connected to OpenHack")
    _html(f"  {GREEN}✓{EGREEN} Inference verified")

    available = set(live_models)
    curated = [item for item in provider["models"] if item[0] in available]
    if not curated:
        curated = [(mid, mid, "Available through OpenHack") for mid in live_models]
    model_idx = await _select_menu_async(
        "Choose your default model",
        curated,
        default_idx=next(
            (i for i, item in enumerate(curated) if item[0] == "glm-5.2"), 0
        ),
    )
    model_id = curated[model_idx if model_idx >= 0 else 0][0]

    new_cfg = {
        "provider": "openhack",
        "model": model_id,
        "openhack_model_id": model_id,
        "openhack_api_key": api_key,
        "onboarding_version": 1,
    }
    if login_result:
        for attr, key in (
            ("org_id", "openhack_org_id"),
            ("org_slug", "openhack_org_slug"),
            ("org_name", "openhack_org_name"),
            ("user_email", "openhack_user_email"),
            ("user_first_name", "openhack_user_first_name"),
            ("user_last_name", "openhack_user_last_name"),
        ):
            value = getattr(login_result, attr, None)
            if value:
                new_cfg[key] = value
    save_user_config(new_cfg)
    reload_settings()
    _html("")
    _html(f"  {GREEN}✓{EGREEN} {B}Security agent ready · {_esc(model_id)}{EB}")
    _html(f"  {DIM}Opening OpenHack…{EDIM}")
    _html("")
    return True


async def _run_wizard(is_first_time: bool = True) -> bool:
    """Run the interactive configuration wizard. Returns True if config was saved."""
    if is_first_time:
        return await _run_first_time_onboarding()

    cfg = load_user_config()

    _setup_banner()

    provider = PROVIDERS[0]
    default_model = provider["default_model"]
    default_base_url = cfg.get("openhack_base_url") or settings.openhack_base_url

    # ── Step 1: Login / API key / Custom ─────────────────────────
    setup_choice = await _select_menu_async(
        "How would you like to proceed?",
        [
            ("login", "Login with OpenHack account", "(Recommended, free $20 credits on signup)"),
            ("apikey", "Use OpenHack API Key", ""),
            ("custom", "Custom setup", ""),
        ],
    )
    if setup_choice < 0:
        _html(f'  {DIM}Setup cancelled.{EDIM}')
        _html("")
        return False

    api_key: Optional[str] = None
    model_id = default_model
    base_url = default_base_url
    login_result = None

    if setup_choice == 0:
        # Browser-based device-code login.
        app_url = cfg.get("openhack_app_url") or settings.openhack_app_url
        try:
            login_result = await device_login(app_url)
            api_key = login_result.token
        except DeviceLoginCancelled:
            _html(f'  {DIM}Login cancelled.{EDIM}')
            _html("")
            return False
        except DeviceLoginExpired as exc:
            _html(f'  {YELLOW}⚠{EYELLOW}  {_esc(str(exc))}')
            _html("")
            return False
        except DeviceLoginError as exc:
            _html(f'  {YELLOW}⚠{EYELLOW}  Login failed: {_esc(str(exc))}')
            _html("")
            return False
    elif setup_choice == 1:
        # User pastes an existing OpenHack API token from the dashboard.
        existing_key = cfg.get(provider["key_field"])
        api_key = await _prompt_api_key(provider, existing_key)
        if not api_key:
            _html("")
            _html(f'  {YELLOW}⚠{EYELLOW}  An API key is required.')
            _html(f'  {DIM}Sign up at: {_esc(settings.openhack_app_url)}/signup{EDIM}')
            _html("")
    else:
        # Custom: base URL, API key, model string.
        _html("")
        _html(f'  {B}OpenAI-Compatible API Endpoint{EB}')
        existing_base = cfg.get("openhack_base_url") or default_base_url
        _html(f'  {DIM}Current: {_esc(existing_base)}{EDIM}')
        _html(f'  {DIM}Press Enter to keep current{EDIM}')
        _html("")
        try:
            url_input = (await _input_async("  Base URL: ")).strip()
        except (EOFError, KeyboardInterrupt):
            _html(f'  {DIM}Setup cancelled.{EDIM}')
            _html("")
            return False
        base_url = url_input if url_input else existing_base

        existing_key = cfg.get(provider["key_field"])
        api_key = await _prompt_api_key(provider, existing_key)
        if not api_key:
            _html("")
            _html(f'  {YELLOW}⚠{EYELLOW}  An API key is required.')
            _html("")

        _html("")
        _html(f'  {B}Model{EB}')
        existing_model = cfg.get("model") or cfg.get("openhack_model_id") or default_model
        _html(f'  {DIM}Current: {_esc(existing_model)}{EDIM}')
        _html(f'  {DIM}Press Enter to keep current{EDIM}')
        _html("")
        try:
            model_input = (await _input_async("  Model: ")).strip()
        except (EOFError, KeyboardInterrupt):
            _html(f'  {DIM}Setup cancelled.{EDIM}')
            _html("")
            return False
        model_id = model_input if model_input else existing_model

    # ── Step 2b: Pick a model (login / API-key paths) ────────────
    # Custom setup already asked for a model string above.
    if setup_choice in (0, 1) and api_key:
        model_id = await _pick_model_async(provider, api_key, base_url, default_model)

    # ── Step 3: Summary & confirm ─────────────────────────────────
    org_name = login_result.org_name if login_result else None
    if not await _show_summary(provider, model_id, api_key, base_url, org_name):
        _html(f'  {DIM}Setup cancelled. No changes saved.{EDIM}')
        _html("")
        return False

    # ── Save ──────────────────────────────────────────────────────
    new_cfg = {
        "provider": "openhack",
        "model": model_id,
        "openhack_model_id": model_id,
    }
    # Only persist base_url if the user explicitly customized it. Otherwise
    # leave it out so the dev/prod default (driven by OPENHACK_DEV) wins.
    if setup_choice == 2 and base_url and base_url != settings.openhack_base_url:
        new_cfg["openhack_base_url"] = base_url
    if api_key:
        new_cfg["openhack_api_key"] = api_key
    if login_result:
        if login_result.org_id:
            new_cfg["openhack_org_id"] = login_result.org_id
        if login_result.org_slug:
            new_cfg["openhack_org_slug"] = login_result.org_slug
        if login_result.org_name:
            new_cfg["openhack_org_name"] = login_result.org_name
        if login_result.user_email:
            new_cfg["openhack_user_email"] = login_result.user_email
        if login_result.user_first_name:
            new_cfg["openhack_user_first_name"] = login_result.user_first_name
        if login_result.user_last_name:
            new_cfg["openhack_user_last_name"] = login_result.user_last_name

    save_user_config(new_cfg)
    reload_settings()

    _html("")
    _html(f'  {GREEN}✓{EGREEN} {B}Configuration saved!{EB}')
    _html(f'  {DIM}Stored in {_esc(str(CONFIG_PATH))}{EDIM}')
    _html("")

    return True


def needs_first_time_setup() -> bool:
    """Check if this is a first-time run (no config file exists)."""
    if not CONFIG_PATH.exists():
        return True
    cfg = load_user_config()
    if not cfg:
        return True
    has_provider = cfg.get("provider")
    if not has_provider:
        return True
    if has_provider != "openhack":
        from openhack import providers as provider_registry
        resolved = provider_registry.resolve(str(has_provider))
        return not resolved or bool(resolved.missing_key_env)
    # The hosted provider keeps its token in the legacy config for compatibility.
    has_any_key = any(
        cfg.get(p["key_field"])
        for p in PROVIDERS
    )
    return not has_any_key


def run_first_time_setup() -> bool:
    """Run the first-time setup wizard. Returns True if setup completed."""
    return asyncio.run(_run_wizard(is_first_time=True))


async def run_setup_command() -> bool:
    """Run the /setup configuration wizard (async, for use inside TUI). Returns True if config was saved."""
    return await _run_wizard(is_first_time=False)


async def run_provider_connect(
    provider_id: Optional[str] = None,
    auth_method: Optional[str] = None,
) -> bool:
    """Connect a BYOK/subscription provider from inside the scanner.

    This is deliberately a suspended terminal flow: API keys are entered with
    password masking and never appear in the transcript or slash-command
    history.
    """
    from openhack import providers as provider_registry
    from openhack.agents.llm import fetch_available_models
    from openhack.model_catalog import merge_models
    from openhack.provider_auth import (
        ProviderAuthError,
        get_credential,
        openai_browser_login,
        openai_device_login,
        set_api_key,
    )

    if not provider_id:
        specs = provider_registry.list_provider_specs(refresh=True)
        items = [
            (spec.name, spec.label, spec.hint or spec.api_key_env)
            for spec in specs
        ]
        idx = await _select_menu_async("Connect a provider", items)
        if idx < 0:
            return False
        provider_id = items[idx][0]

    provider_id = provider_id.lower().strip()
    if provider_id == "openhack":
        return await run_setup_command()
    spec = provider_registry.get_spec(provider_id)
    if spec is None:
        _html(f"  {YELLOW}⚠{EYELLOW} Unknown provider: {_esc(provider_id)}")
        return False

    if provider_id == "openai" and not auth_method:
        idx = await _select_menu_async(
            "Connect OpenAI",
            [
                ("browser", "ChatGPT Plus/Pro (browser)", "Use your ChatGPT subscription"),
                ("device", "ChatGPT Plus/Pro (headless)", "Open a URL and enter a code"),
                ("api", "OpenAI API key", "Billed through the API platform"),
            ],
        )
        if idx < 0:
            return False
        auth_method = ("browser", "device", "api")[idx]
    auth_method = (auth_method or "api").lower()

    try:
        if provider_id == "openai" and auth_method == "browser":
            _html("")
            _html(f"  {B}Opening OpenAI login in your browser…{EB}")
            _html(f"  {DIM}Waiting for the secure localhost callback on port 1455.{EDIM}")
            await asyncio.to_thread(openai_browser_login)
        elif provider_id == "openai" and auth_method == "device":
            def show_code(url: str, code: str) -> None:
                _html("")
                _html(f"  Open {_esc(url)}")
                _html(f"  Enter code: {B}{_esc(code)}{EB}")
                _html(f"  {DIM}Waiting for authorization…{EDIM}")

            await asyncio.to_thread(openai_device_login, on_code=show_code)
        else:
            existing = get_credential(provider_id) or {}
            current = str(existing.get("key") or "")
            _html("")
            _html(f"  {B}API key for {_esc(spec.label)}{EB}")
            _html(f"  {DIM}Environment variable: {_esc(spec.api_key_env)}{EDIM}")
            if current:
                _html(f"  {DIM}Current: {_esc(_mask_key(current))}{EDIM}")
            key = (await _input_async("  API Key: ", is_password=True)).strip()
            if not key:
                key = current or os.environ.get(spec.api_key_env, "")
            if not key:
                _html(f"  {YELLOW}⚠{EYELLOW} No API key entered.")
                return False
            set_api_key(provider_id, key)
    except ProviderAuthError as exc:
        _html(f"  {YELLOW}⚠{EYELLOW} Connection failed: {_esc(str(exc))}")
        return False

    resolved = provider_registry.resolve(provider_id)
    if resolved is None:
        return False
    live = None
    if resolved.auth_type != "oauth":
        live = fetch_available_models(
            api_key=resolved.api_key,
            base_url=resolved.base_url,
            timeout=5,
        )
    models = merge_models(provider_id, live)
    model_id = resolved.model
    if models:
        items = [
            (model["id"], model["label"], model.get("desc", ""))
            for model in models
        ]
        default_idx = next(
            (i for i, item in enumerate(items) if item[0] == model_id), 0
        )
        idx = await _select_menu_async("Choose a model", items, default_idx)
        if idx >= 0:
            model_id = items[idx][0]

    save_user_config({"provider": provider_id, "model": model_id})
    reload_settings()
    _html("")
    _html(f"  {GREEN}✓{EGREEN} Connected {_esc(spec.label)} · {_esc(model_id)}")
    _html("")
    return True
