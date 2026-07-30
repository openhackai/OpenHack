"""
Interactive TUI for OpenHack.

Full-screen prompt_toolkit Application with two modes:

- LANDING: ground-symbol logo + "OpenHack" wordmark + centered input. Tip and
  account footer below. Type a slash command or a path/URL to scan.
- SCANNING: pinned status bar (target, elapsed, cost) + VSplit pane layout
  (agents on the left, findings on the right) + input bar at the bottom.

Scan execution still uses CoordinatorAgent/Session under the hood; trace
events from the session are translated into agent/finding pane state and the
layout re-renders on every update.
"""

import asyncio
import json
import logging
import math
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from prompt_toolkit import HTML
from prompt_toolkit.data_structures import Point
from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import to_formatted_text
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import (
    ConditionalContainer,
    Float,
    FloatContainer,
    HSplit as _PromptToolkitHSplit,
    VSplit as _PromptToolkitVSplit,
    Window,
    WindowAlign,
)
from prompt_toolkit.layout.scrollable_pane import ScrollablePane
from prompt_toolkit.widgets import Frame
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.formatted_text import split_lines
from prompt_toolkit.layout.dimension import Dimension as D
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.layout.processors import BeforeInput, Processor, Transformation
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.styles import Style

from openhack import __version__ as OPENHACK_VERSION
from openhack.agents.coordinator import CoordinatorAgent
from openhack.agents.llm import LLMClient, Message, LLMResponse
from openhack.agents.session import Session, SessionStatus, Finding, TraceEntry
from openhack.agents.eventlog import redact

logger = logging.getLogger(__name__)
from openhack.config import (
    settings,
    save_user_config,
    load_user_config,
    resolve_provider,
    reload_settings,
    _PROVIDER_KEY_FIELDS,
)
from openhack.setup import run_provider_connect, run_setup_command
from openhack.shells import ShellManager
from openhack.tools.registry import ToolRegistry
from openhack.prompts.project_context import build_project_context
from openhack.updates import Announcement, UpdateInfo, fetch_updates, save_dismissed


def _small_window_fallback() -> Window:
    """Blank fallback for geometry that cannot fit; never block with a warning."""
    return Window(
        content=FormattedTextControl(text=[]),
        char=" ",
        style="class:body",
    )


class HSplit(_PromptToolkitHSplit):
    """HSplit without prompt_toolkit's intrusive size-warning replacement."""

    def __init__(self, children, **kwargs):
        kwargs.setdefault("window_too_small", _small_window_fallback())
        super().__init__(children, **kwargs)


class VSplit(_PromptToolkitVSplit):
    """VSplit without prompt_toolkit's intrusive size-warning replacement."""

    def __init__(self, children, **kwargs):
        kwargs.setdefault("window_too_small", _small_window_fallback())
        super().__init__(children, **kwargs)


# ── OpenHack palette ──────────────────────────────────────────────
# The brand system: tinted dark neutrals (cool green family, never pure
# black/white) carrying one signal-green accent used with restraint, plus a
# warm coral as the secondary. Values are the resolved brand tokens.
OH_BG        = "#000000"  # solid black canvas
OH_PANEL     = "#0A0A0A"  # near-black sidebar / elevated surface (neutral)
OH_ELEM      = "#101010"  # element surface (input box / message box) — neutral
OH_TEXT      = "#E8EAE9"  # Chalk — primary foreground (cool near-white)
OH_MUTED     = "#7E8784"  # Stone — secondary / dimmed text
OH_BORDER    = "#2A2A2A"  # default border (neutral grey, no green tint)
OH_BORDER_A  = "#3A3A3A"  # active border (neutral)
OH_BORDER_SUB= "#181818"  # subtle border / divider (neutral)
OH_PRIMARY   = "#00B97E"  # Signal Green — THE accent (brand, used sparingly)
OH_SECONDARY = "#00B97E"  # accent bars + spinner share the one signal green
OH_ACCENT    = "#1CC584"  # brighter green (headings / keywords)
OH_RED       = "#EA6A64"  # warm coral — errors (brand secondary)
OH_ORANGE    = "#E99B2A"  # warning
OH_GREEN     = "#00B97E"  # success == the signal green
OH_CYAN      = "#5BB39E"  # soft teal (info, kept in the green family)
OH_YELLOW    = "#DEBA50"
OH_USER_BG   = "#1A1D1F"  # subtle grey band behind the user's own messages


# ── Brand ─────────────────────────────────────────────────────────

# The OpenHack mark — the ground/earth symbol: a tall thin stem (half the
# total height) descending into three bars that narrow as they go down, with a
# one-unit gap between each bar. Proportions trace the brand SVG (stem 1u wide
# ×5u tall; bars 9u/7u/4u wide ×1u thick; gaps 1u). Drawn on an 18-col grid so
# every element is centered on the same axis; per-line centering keeps it so.
_MARK_ROWS = [
    "        ██        ",
    "        ██        ",
    "        ██        ",
    "        ██        ",
    "        ██        ",
    "██████████████████",
    "                  ",
    "  ██████████████  ",
    "                  ",
    "     ████████     ",
]

# The "OpenHack" wordmark is rendered as plain bold text beneath the mark — a
# clean logo lockup (large green symbol over a simple wordmark).
_WORDMARK = "OpenHack"


# ── Knight-rider spinner ──────────────────────────────────────────
# A smooth single-cell braille spinner shown while the agent/scan is working.
# Ten frames of a rotating dot — clean, legible, and renders in any terminal.
_SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

# Codex-style status shimmer.  The sweep travels through the label and the
# surrounding padding every two seconds, so it enters and leaves cleanly
# instead of jumping from the last character back to the first.
_SHIMMER_PADDING = 10
_SHIMMER_PERIOD_SECONDS = 2.0
_SHIMMER_HALF_WIDTH = 5.0


def _rgb(hex_color: str) -> tuple[int, int, int]:
    value = hex_color.removeprefix("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def _blend_rgb(
    highlight: tuple[int, int, int],
    base: tuple[int, int, int],
    intensity: float,
) -> tuple[int, int, int]:
    alpha = max(0.0, min(1.0, intensity))
    return tuple(
        int(fg * alpha + bg * (1.0 - alpha))
        for fg, bg in zip(highlight, base)
    )


def _shimmer_fragments(
    text: str,
    *,
    elapsed: Optional[float] = None,
) -> list[tuple[str, str]]:
    """Render ``text`` as a smooth, time-based per-character highlight."""
    if not text:
        return []

    elapsed = time.monotonic() if elapsed is None else elapsed
    period = len(text) + _SHIMMER_PADDING * 2
    position = (
        (elapsed % _SHIMMER_PERIOD_SECONDS)
        / _SHIMMER_PERIOD_SECONDS
        * period
    )
    base = _rgb(OH_MUTED)
    highlight = _rgb(OH_TEXT)
    fragments: list[tuple[str, str]] = []

    for index, char in enumerate(text):
        distance = abs(index + _SHIMMER_PADDING - position)
        if distance <= _SHIMMER_HALF_WIDTH:
            x = math.pi * distance / _SHIMMER_HALF_WIDTH
            intensity = 0.5 * (1.0 + math.cos(x))
        else:
            intensity = 0.0
        red, green, blue = _blend_rgb(highlight, base, intensity * 0.9)
        fragments.append((f"bold fg:#{red:02x}{green:02x}{blue:02x}", char))

    return fragments


def _abbrev_home(path: str) -> str:
    """`/Users/x/code` → `~/code` for compact display."""
    try:
        home = str(Path.home())
        if path == home:
            return "~"
        if path.startswith(home + os.sep):
            return "~" + path[len(home):]
    except Exception:
        pass
    return path


def _resolve_path_argument(raw: str, *, default: Optional[str] = None) -> Path:
    """Resolve a CLI/TUI path, accepting the ``@path`` mention syntax."""
    value = (raw or "").strip()
    if value.startswith("@"):
        value = value[1:].strip()
    if not value:
        value = default or os.getcwd()
    return Path(os.path.expanduser(value)).resolve()


def _git_branch(path: str) -> str:
    """Current git branch for `path`, or "" if not a repo."""
    try:
        head = Path(path) / ".git" / "HEAD"
        # Walk up to find the repo root's .git/HEAD.
        cur = Path(path)
        for _ in range(8):
            h = cur / ".git" / "HEAD"
            if h.exists():
                ref = h.read_text().strip()
                if ref.startswith("ref:"):
                    return ref.rsplit("/", 1)[-1]
                return ref[:7]
            if cur.parent == cur:
                break
            cur = cur.parent
    except Exception:
        pass
    return ""


def _fmt_tokens(n: int) -> str:
    """6640 → '6.6K', 1500000 → '1.5M'."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


class _PlaceholderProcessor(Processor):
    """Show dim placeholder text in an empty single-line buffer."""

    def __init__(self, text_fn: Callable[[], str], style: str = "class:input.placeholder"):
        self.text_fn = text_fn
        self.style = style

    def apply_transformation(self, ti):
        if not ti.document.text and ti.lineno == 0:
            return Transformation([(self.style, self.text_fn())])
        return Transformation(ti.fragments)



PROVIDER_DEFAULTS = {"openhack": "grok-4.5"}

# Models the OpenHack hosted provider serves (must match the inference backend's
# MODEL_MAP). Shown by `/model` so users can discover what they can switch to.
# mistral-large-2512 was dropped: the inference backend now permits only
# US-headquartered providers, and Mistral (FR) is its sole OpenRouter provider,
# so every request would 400 as an unknown model. Offering it in the picker
# would just be a guaranteed failure.
from openhack.model_catalog import OPENHACK_MODELS as _OPENHACK_MODEL_ROWS

OPENHACK_MODELS = [row[0] for row in _OPENHACK_MODEL_ROWS]

# Display label + one-line description per served model, for the /model picker.
OPENHACK_MODEL_INFO = {
    "grok-4.5": ("Grok 4.5", "Frontier model by xAI · strongest exploitation · default"),
    "glm-5.2": ("GLM 5.2", "Reasoning model by Z.ai · fast & cost-efficient"),
    "kimi-k2.5": ("Kimi K2.5", "Flagship security model by Moonshot"),
    "gemma-4-31b": ("Gemma 4 31B", "Open-weight model by Google · free"),
    "mistral-large-2512": ("Mistral Large", "Open-weight dense model by Mistral"),
}

CHAT_SYSTEM_PROMPT = (
    "You are OpenHack, a security-focused AI assistant embedded in the OpenHack CLI. "
    "You help users understand vulnerability scan results, explain security concepts, "
    "and advise on remediation. Be concise and direct. "
    "If the user asks you to scan, tell them to use /scan <path>."
)


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ── Severity coloring ─────────────────────────────────────────────

def _sev_style(severity: str) -> str:
    s = (severity or "").lower()
    if s == "critical":
        return "class:sev.critical"
    if s == "high":
        return "class:sev.high"
    if s == "medium":
        return "class:sev.medium"
    if s == "low":
        return "class:sev.low"
    return "class:sev.info"


def _sev_label(severity: str) -> str:
    s = (severity or "").lower()
    return {
        "critical": "CRIT",
        "high": "HIGH",
        "medium": "MED ",
        "low": "LOW ",
    }.get(s, "INFO")


# ── Slash command registry ────────────────────────────────────────

_SLASH_COMMANDS = [
    ("/plan", "Draft a read-only attack plan for a target/objective"),
    ("/scan", "Run the full multi-agent scan pipeline on a directory (defaults to current)"),
    ("/cd", "Change the working directory — /cd <path>"),
    ("/findings", "Show findings from the current session"),
    ("/verify", "Verify loaded findings (`/verify sandbox` or `/verify browser`)"),
    ("/copy", "Copy the selected finding as an AI-fix prompt to the clipboard"),
    ("/pause", "Pause the running scan/agent (Ctrl+C also pauses)"),
    ("/resume", "Resume a paused run"),
    ("/cancel", "Stop the running scan/agent"),
    ("/clear", "Clear the conversation / return to landing"),
    ("/sessions", "Browse and re-load past scan results"),
    ("/bashes", "Watch and kill background shells (started with !cmd &)"),
    ("/provider", "Switch LLM provider (openhack, openai, anthropic, …)"),
    ("/connect", "Connect a provider or ChatGPT Plus/Pro subscription"),
    ("/disconnect", "Remove saved credentials for a provider"),
    ("/model", "Set or show the model ID"),
    ("/config", "Show or set configuration"),
    ("/cost", "Show cost + tokens for the current session"),
    ("/mouse", "Toggle mouse capture (off = drag-to-select text)"),
    ("/login", "Sign in to your OpenHack account"),
    ("/logout", "Sign out (clears the saved token)"),
    ("/setup", "Run the setup wizard"),
    ("/discord", "Open the OpenHack Discord in your browser"),
    ("/help", "Show available commands"),
    ("/quit", "Exit"),
]

_CONFIG_KEYS = [
    ("provider", "LLM provider"),
    ("model", "Model ID override"),
    ("openhack_api_key", "OpenHack API key"),
    ("openhack_model_id", "OpenHack model ID"),
]

_CANCEL_PHRASES = {
    "cancel", "cancel scan", "cancel the scan",
    "stop", "stop scan", "stop the scan",
    "abort", "abort scan",
}


class OpenHackCompleter(Completer):
    # Directories skipped when indexing files for @-references.
    _AT_SKIP_DIRS = {
        ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
        ".next", ".nuxt", ".output", "vendor", "target", "coverage",
        ".mypy_cache", ".pytest_cache", ".tox", ".idea", ".vscode",
        ".openhack-evidence",
    }
    _AT_CAP = 6000  # max entries indexed

    def __init__(self) -> None:
        self._at_index: Optional[list[tuple[str, bool]]] = None

    # ── @path references (OpenCode-style file/dir picker) ─────────────
    @staticmethod
    def _active_at_token(text: str) -> Optional[str]:
        """If the cursor is in an '@<partial>' token, return <partial>, else None.

        The '@' must start a token (input start or after whitespace), and the
        partial can't contain whitespace (we're still typing the path).
        """
        at = text.rfind("@")
        if at == -1:
            return None
        if at > 0 and not text[at - 1].isspace():
            return None
        frag = text[at + 1:]
        if any(c.isspace() for c in frag):
            return None
        return frag

    def _build_at_index(self) -> list[tuple[str, bool]]:
        """Walk the cwd once, collecting (relative_path, is_dir) for @ matching."""
        entries: list[tuple[str, bool]] = []
        root = Path.cwd()
        try:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [
                    d for d in dirnames
                    if d not in self._AT_SKIP_DIRS and not d.startswith(".")
                ]
                rel_dir = os.path.relpath(dirpath, root)
                for d in sorted(dirnames):
                    p = d if rel_dir == "." else f"{rel_dir}/{d}"
                    entries.append((p + "/", True))
                for f in sorted(filenames):
                    if f.startswith("."):
                        continue
                    p = f if rel_dir == "." else f"{rel_dir}/{f}"
                    entries.append((p, False))
                if len(entries) >= self._AT_CAP:
                    break
        except OSError:
            pass
        return entries

    def _path_completions(self, partial: str):
        # Navigational paths (parent dir, absolute, home) browse the filesystem
        # directly — the cwd index can't see outside the working tree.
        if partial in ("..", "."):
            yield Completion(
                "@" + partial + "/", start_position=-(len(partial) + 1),
                display=partial + "/", display_meta="dir",
            )
            return
        if partial.startswith(("../", "./", "/", "~")):
            yield from self._fs_listing(partial)
            return

        if self._at_index is None:
            self._at_index = self._build_at_index()
        q = partial.lower()
        scored: list[tuple[int, int, str, bool]] = []
        for path, is_dir in self._at_index:
            name = path.rstrip("/").rsplit("/", 1)[-1].lower()
            full = path.lower()
            if not q:
                # No query yet: show top-level entries first (dirs before files).
                depth = full.rstrip("/").count("/")
                score = depth * 2 + (0 if is_dir else 1)
            elif name.startswith(q):
                score = 0
            elif q in name:
                score = 2
            elif q in full:
                score = 4
            else:
                continue
            scored.append((score, len(path), path, is_dir))
        scored.sort(key=lambda r: (r[0], r[1], r[2]))
        replace = -(len(partial) + 1)  # also replace the leading '@'
        for _score, _len, path, is_dir in scored[:30]:
            yield Completion(
                "@" + path,
                start_position=replace,
                display=path,
                display_meta="dir" if is_dir else "file",
            )

    def _fs_listing(self, partial: str):
        """Browse the filesystem for a navigational @path (../, ./, /, ~).

        Lists the immediate children of the directory portion of `partial`,
        filtered by the name prefix being typed — directories first — so the
        user can walk up (../) and into sibling trees.
        """
        # Split into the directory portion and the name prefix being typed.
        if partial.endswith("/"):
            dir_part, name_prefix = partial, ""
        else:
            cut = partial.rfind("/")
            dir_part = partial[: cut + 1] if cut >= 0 else ""
            name_prefix = partial[cut + 1:] if cut >= 0 else partial

        expanded = os.path.expanduser(dir_part) if dir_part else "."
        try:
            base = Path(expanded)
            if not base.is_dir():
                return
            children = sorted(
                base.iterdir(), key=lambda p: (p.is_file(), p.name.lower())
            )
        except OSError:
            return

        want = name_prefix.lower()
        shown = 0
        for child in children:
            name = child.name
            if name.startswith(".") and not name_prefix.startswith("."):
                continue
            if want and not name.lower().startswith(want):
                continue
            try:
                is_dir = child.is_dir()
            except OSError:
                is_dir = False
            path = dir_part + name + ("/" if is_dir else "")
            yield Completion(
                "@" + path, start_position=-(len(partial) + 1),
                display=path, display_meta="dir" if is_dir else "file",
            )
            shown += 1
            if shown >= 40:
                return

    def get_completions(self, document: Document, complete_event):
        text = document.text_before_cursor

        # @path reference works anywhere in the message (even mid-sentence).
        at_token = self._active_at_token(text)
        if at_token is not None:
            yield from self._path_completions(at_token)
            return

        words = text.split()

        if not text or (len(words) == 1 and not text.endswith(" ")):
            prefix = text.lstrip()
            # Only surface the command list once the user starts a command with
            # "/" — an empty box shouldn't dump every command into a popup.
            if prefix.startswith("/"):
                for cmd, desc in _SLASH_COMMANDS:
                    if cmd.startswith(prefix):
                        yield Completion(cmd, start_position=-len(prefix), display_meta=desc)
            return

        # Whitespace-only input (no actual words) — nothing to complete against.
        if not words:
            return

        cmd = words[0].lower()

        if cmd == "/config":
            if len(words) == 1 and text.endswith(" "):
                for key, desc in _CONFIG_KEYS:
                    yield Completion(key, display_meta=desc)
            elif len(words) == 2 and not text.endswith(" "):
                partial = words[1]
                for key, desc in _CONFIG_KEYS:
                    if key.startswith(partial):
                        yield Completion(key, start_position=-len(partial), display_meta=desc)
        elif cmd == "/scan":
            partial = words[-1] if len(words) > 1 and not text.endswith(" ") else ""
            base = partial or "."
            try:
                base_path = Path(base)
                if base_path.is_dir():
                    parent = base_path
                    prefix = ""
                else:
                    parent = base_path.parent if base_path.parent.is_dir() else Path(".")
                    prefix = base_path.name
                for child in sorted(parent.iterdir()):
                    if child.name.startswith(".") or not child.is_dir():
                        continue
                    if prefix and not child.name.startswith(prefix):
                        continue
                    yield Completion(str(child) + "/", start_position=-len(partial))
            except OSError:
                pass
        elif cmd in ("/cd", "/cwd"):
            # Directory-only completion (cd only takes dirs), mirroring /scan.
            # `@dir` mentions also work — the @-completer handles those and
            # _cmd_cd strips the leading '@'.
            partial = words[-1] if len(words) > 1 and not text.endswith(" ") else ""
            base = os.path.expanduser(partial) if partial else "."
            try:
                base_path = Path(base)
                if base_path.is_dir() and (not partial or partial.endswith(("/", "~"))):
                    parent, prefix = base_path, ""
                else:
                    parent = base_path.parent if base_path.parent.is_dir() else Path(".")
                    prefix = base_path.name
                for child in sorted(parent.iterdir()):
                    if child.name.startswith(".") or not child.is_dir():
                        continue
                    if prefix and not child.name.startswith(prefix):
                        continue
                    # Preserve the user's typed prefix style (e.g. ~/ , ../).
                    typed_dir = partial[: partial.rfind("/") + 1] if "/" in partial else ""
                    yield Completion(
                        typed_dir + child.name + "/", start_position=-len(partial),
                        display=child.name + "/", display_meta="dir",
                    )
            except OSError:
                pass


# ── Agent/finding state derived from trace entries ────────────────

# Status icons:
#   ◌  pending (not yet started)
#   ●  running (current step is this agent)
#   ▸  working (mid-task)
#   ✓  complete
#   ✗  failed / cancelled

_STATUS_PENDING = ("◌", "class:status.pending")
_STATUS_RUNNING = ("●", "class:status.running")
_STATUS_WORKING = ("▸", "class:status.working")
_STATUS_DONE = ("✓", "class:status.done")
_STATUS_FAIL = ("✗", "class:status.fail")


class _AgentRow:
    __slots__ = ("name", "status", "detail")

    def __init__(self, name: str, status: tuple[str, str], detail: str = ""):
        self.name = name
        self.status = status
        self.detail = detail


class ScanState:
    """Derived UI state for an in-progress scan."""

    def __init__(self, target: str):
        self.target = target
        self.start_time = time.time()
        self.end_time: Optional[float] = None  # set when the scan terminates
        self.cost: float = 0.0
        self.current_step: Optional[str] = None
        self.agents: dict[str, _AgentRow] = {}
        self.findings: list[Finding] = []
        self.agent_order: list[str] = []
        self.last_message: str = ""
        # Each rendered trace line carries its source agent so the Trace tab
        # can filter to "show only this agent's events".
        self.trace_lines: list[tuple[str, list[tuple[str, str]]]] = []
        # Unique agents in order of first appearance — drives the trace sidebar.
        self.trace_agents: list[str] = []
        # Index of the interactive agent's last tool-call line, so its result can
        # be folded into that same line (one row per tool call, not two).
        self._pending_tool_idx: Optional[int] = None

    def _append_trace(self, agent: str, fragments: list[tuple[str, str]]) -> None:
        """Internal: record a rendered trace line with its agent attribution."""
        self.trace_lines.append((agent, fragments))
        if agent and agent not in self.trace_agents:
            self.trace_agents.append(agent)

    def finish(self) -> None:
        """Freeze the elapsed clock — call when the scan completes/cancels/fails."""
        if self.end_time is None:
            self.end_time = time.time()

    def elapsed_str(self) -> str:
        endpoint = self.end_time if self.end_time is not None else time.time()
        seconds = int(endpoint - self.start_time)
        m, s = divmod(seconds, 60)
        return f"{m}:{s:02d}" if m else f"0:{s:02d}"

    def upsert_agent(self, name: str, status: tuple[str, str], detail: str = "") -> None:
        row = self.agents.get(name)
        if row is None:
            self.agents[name] = _AgentRow(name, status, detail)
            self.agent_order.append(name)
        else:
            row.status = status
            if detail:
                row.detail = detail

    def update_from_trace(self, entry: TraceEntry) -> None:
        agent = entry.agent
        etype = entry.event_type

        ts = self._ts(entry.timestamp)

        if etype == "user":
            content_str = str(entry.content or "").strip()
            if content_str:
                self.last_message = f"you · {content_str[:60]}"
                if self.trace_lines:
                    self._append_trace(agent, [("", "")])  # blank line between messages
                line: list[tuple[str, str]] = [
                    ("class:trace.time", ts),
                    ("class:trace.user.bar", " ▌ "),
                    # Trailing pad so the grey band reads as a band, not tight text.
                    ("class:trace.user", content_str + "  "),
                ]
                self._append_trace(agent, line)
            return

        if etype == "step_start":
            self.current_step = str(entry.content)
            self.last_message = f"step start · {entry.content}"
            self._append_trace(agent, [
                ("class:trace.time", ts),
                ("class:trace.step", f"  ── {entry.content} ──"),
            ])
            return

        if etype == "step_complete":
            data = entry.content if isinstance(entry.content, dict) else {}
            self.cost += float(data.get("cost", 0) or 0)
            self.last_message = f"step complete · {data.get('step', '')}"
            self._append_trace(agent, [
                ("class:trace.time", ts),
                ("class:trace.dim", f"  {data.get('step', 'step')} complete · "
                 f"${float(data.get('cost', 0)):.4f} · {data.get('tokens', 0):,} tok"),
            ])
            return

        if etype == "swarm_start":
            data = entry.content if isinstance(entry.content, dict) else {}
            groups = data.get("groups", [])
            base = agent.replace("_swarm", "")
            for g in groups:
                self.upsert_agent(f"{base}:{g}", _STATUS_PENDING, "queued")
            self.last_message = f"{agent} · spawned {len(groups)} sub-agents"
            count = data.get("group_count") or data.get("findings_count") or len(groups)
            self._append_trace(agent, [
                ("class:trace.time", ts),
                ("class:trace.agent", f"  {agent}"),
                ("class:trace.dim", f" spawned {count} sub-agents"),
            ])
            return

        if etype == "swarm_complete":
            data = entry.content if isinstance(entry.content, dict) else {}
            base = agent.replace("_swarm", "")
            for name in list(self.agents):
                if name.startswith(f"{base}:") and self.agents[name].status[0] != "✓":
                    self.upsert_agent(name, _STATUS_DONE, "complete")
            cost = data.get("total_cost", 0)
            self.cost += float(cost or 0)
            n = data.get("total_findings") or data.get("total_confirmed") or 0
            self.last_message = f"{agent} · complete"
            self._append_trace(agent, [
                ("class:trace.time", ts),
                ("class:trace.agent", f"  {agent}"),
                ("class:trace.dim", f" complete · {n} findings · ${float(cost):.4f}"),
            ])
            return

        if etype == "tool_call":
            tool = entry.tool_name or "tool"
            # finish_task is an internal lifecycle signal, not an operator
            # action. Keep it in the durable event journal, never in chat.
            if tool == "finish_task":
                return
            args = entry.tool_input or {}
            detail = _short_tool_label(tool, args)
            self.upsert_agent(agent, _STATUS_WORKING, detail)
            self.last_message = f"{detail}"
            if str(agent).startswith("openhack"):
                # Interactive agent: a tool call is a quiet sub-action beneath the
                # conversation — no agent name, no arrow, just an indented tool tag.
                # Its result is folded onto this same row when it lands.
                self._append_trace(agent, [
                    ("class:trace.time", ts),
                    ("class:trace.tool.dot", "  → "),
                    ("class:trace.tool.name", tool),
                    ("class:trace.dim", f"  {detail}" if detail and detail != tool else ""),
                ])
                self._pending_tool_idx = len(self.trace_lines) - 1
            else:
                # Scan pipeline: many named agents, so keep the attribution.
                self._append_trace(agent, [
                    ("class:trace.time", ts),
                    ("class:trace.agent", f"  {agent:>24}"),
                    ("class:trace.arrow", "  →  "),
                    ("class:trace.tool", tool),
                    ("class:trace.dim", f"  {detail}" if detail and detail != tool else ""),
                ])
            return

        if etype == "tool_result":
            if entry.tool_name == "finish_task":
                return
            row = self.agents.get(agent)
            if row and row.status[0] == "▸":
                row.status = _STATUS_RUNNING
            # For the interactive agent, show a compact one-line result under the
            # tool call so the operator sees the outcome (exit code / count /
            # error). Full output is preserved in the session JSON.
            if str(agent).startswith("openhack"):
                summary = _summarize_tool_output(entry.tool_output)
                if summary:
                    summary = " ".join(str(summary).split())
                    if len(summary) > 90:
                        summary = summary[:89] + "…"
                    idx = self._pending_tool_idx
                    # Fold the outcome onto the tool-call row it belongs to.
                    if idx is not None and 0 <= idx < len(self.trace_lines):
                        style = ("class:trace.fail" if summary.startswith("error")
                                 else "class:trace.dim")
                        self.trace_lines[idx][1].append((style, f"  · {summary}"))
                    else:
                        self._append_trace(agent, [
                            ("class:trace.time", ts),
                            ("class:trace.dim", "     " + summary),
                        ])
                self._pending_tool_idx = None
            return

        if etype == "thinking":
            self.upsert_agent(agent, _STATUS_RUNNING, "thinking…")
            content_str = str(entry.content or "").strip()
            if content_str:
                is_interactive = str(agent).startswith("openhack")
                # Pipeline-agent progress can be extremely verbose and is only
                # an activity trace, so keep that bounded. Interactive agent
                # text is the operator-facing answer: never truncate it here.
                # The transcript viewport already scrolls and the durable report
                # contains the same complete text.
                if not is_interactive and len(content_str) > 2000:
                    content_str = content_str[:1997] + "…"
                if is_interactive:
                    # Interactive agent: render as a clean chat message with a
                    # green speaker bar (mirrors the grey user bar), no name.
                    if self.trace_lines:
                        self._append_trace(agent, [("", "")])  # blank line between messages
                    line: list[tuple[str, str]] = [
                        ("class:trace.time", ts),
                        ("class:trace.agent.bar", " ▌ "),
                    ]
                else:
                    # Scan pipeline: many named agents, so keep the name label.
                    line = [
                        ("class:trace.time", ts),
                        ("class:trace.agent", f"  {agent:>24}"),
                        ("class:trace.arrow", "  ⋯  "),
                    ]
                line.extend(_render_markdown_with_code(content_str))
                self._append_trace(agent, line)
            return

        if etype == "finding_added":
            data = entry.content if isinstance(entry.content, dict) else {}
            sev = (data.get("severity") or "info").lower()
            title = data.get("title", "")
            file_path = data.get("file_path", "")
            self.last_message = f"finding · {title}"
            self._append_trace(agent, [
                ("class:trace.time", ts),
                ("", "  "),
                (_sev_style(sev), f"★ {_sev_label(sev)}"),
                ("", "  "),
                ("class:finding.title", title),
                ("class:finding.path", f"  {file_path}" if file_path else ""),
            ])
            return

        if etype == "queued":
            data = entry.content if isinstance(entry.content, dict) else {}
            title = data.get("title", "")
            self.upsert_agent(agent, _STATUS_PENDING, "queued")
            self.last_message = f"{agent} · queued"
            self._append_trace(agent, [
                ("class:trace.time", ts),
                ("class:trace.agent", f"  {agent:>24}"),
                ("class:trace.dim", f"  queued · {title}" if title else "  queued"),
            ])
            return

        if etype == "sandbox_starting":
            msg = str(entry.content or "starting sandbox…")
            self.last_message = f"{agent} · starting sandbox"
            self._append_trace(agent, [
                ("class:trace.time", ts),
                ("class:trace.agent", f"  {agent}"),
                ("class:trace.dim", f"  {msg}"),
            ])
            return

        if etype == "sandbox_ready":
            data = entry.content if isinstance(entry.content, dict) else {}
            url = data.get("base_url", "")
            self.last_message = f"sandbox ready · {url}"
            self._append_trace(agent, [
                ("class:trace.time", ts),
                ("class:trace.agent", f"  {agent}"),
                ("class:trace.dim", f"  sandbox ready · {url}"),
            ])
            return

        if etype == "error":
            msg = str(entry.content or "error")
            self.upsert_agent(agent, _STATUS_FAIL, msg[:60])
            self.last_message = f"{agent} · error"
            self._append_trace(agent, [
                ("class:trace.time", ts),
                ("class:trace.agent", f"  {agent:>24}"),
                ("class:trace.arrow", "  ✗  "),
                ("class:status.fail", msg[:200]),
            ])
            return

        if etype == "skipped":
            msg = str(entry.content or "skipped")
            self.upsert_agent(agent, _STATUS_FAIL, "skipped")
            self._append_trace(agent, [
                ("class:trace.time", ts),
                ("class:trace.agent", f"  {agent:>24}"),
                ("class:trace.dim", f"  skipped · {msg[:120]}"),
            ])
            return

        if etype == "swarm_aborted":
            msg = str(entry.content or "aborted")
            self.last_message = f"{agent} · aborted"
            self._append_trace(agent, [
                ("class:trace.time", ts),
                ("class:trace.agent", f"  {agent}"),
                ("class:trace.arrow", "  ✗  "),
                ("class:status.fail", msg[:200]),
            ])
            return

        if etype == "sandbox_teardown":
            msg = str(entry.content or "stopping sandbox")
            self.last_message = f"{agent} · teardown"
            self._append_trace(agent, [
                ("class:trace.time", ts),
                ("class:trace.agent", f"  {agent}"),
                ("class:trace.dim", f"  {msg}"),
            ])
            return

        # ── Bang / shell mode ──────────────────────────────────────
        if etype == "shell_start":
            cmd = str(entry.content or "")
            self.last_message = f"shell · {cmd[:60]}"
            if self.trace_lines:
                self._append_trace(agent, [("", "")])  # spacer before a new command
            self._append_trace(agent, [
                ("class:trace.time", ts),
                ("class:trace.user.bar", " ▌ "),
                ("class:trace.shell.cmd", f"$ {cmd}"),
            ])
            return

        if etype == "shell_output":
            self._append_trace(agent, [
                ("class:trace.time", ts),
                ("class:trace.shell", f"  {entry.content}"),
            ])
            return

        if etype == "shell_end":
            data = entry.content if isinstance(entry.content, dict) else {}
            if data.get("interrupted"):
                note = "interrupted"
            elif "error" in data:
                note = f"error · {data['error']}"
            else:
                note = f"exit {data.get('exit_code', 0)}"
            self._append_trace(agent, [
                ("class:trace.time", ts),
                ("class:trace.dim", f"  ↳ {note}"),
            ])
            return

        if etype == "shell_bg":
            data = entry.content if isinstance(entry.content, dict) else {}
            self._append_trace(agent, [
                ("class:trace.time", ts),
                ("class:trace.shell.cmd", f"  {data.get('id', 'sh?')} "),
                ("class:trace.dim", f"started in background · {data.get('command', '')}"),
            ])
            return

    def _ts(self, t: float) -> str:
        # Per-line elapsed timer removed — the trace reads cleaner without the
        # [m:ss] gutter. (Kept as a no-op so the ~20 render sites stay intact;
        # the pinned status bar still shows total elapsed.)
        return ""


# ── Syntax highlighting ───────────────────────────────────────────

def _highlight_code(code: str, file_path: str = "") -> list[tuple[str, str]]:
    """Tokenize *code* with Pygments and return prompt_toolkit fragments."""
    if not code:
        return []
    try:
        from pygments.lexers import get_lexer_for_filename, guess_lexer
        from pygments.token import Token
        from pygments.util import ClassNotFound
    except ImportError:
        return [("class:code", code)]

    lexer = None
    if file_path:
        try:
            lexer = get_lexer_for_filename(file_path)
        except ClassNotFound:
            lexer = None
    if lexer is None:
        try:
            lexer = guess_lexer(code)
        except Exception:
            return [("class:code", code)]

    def style_for(token) -> str:
        if token in Token.Comment:
            return "class:syntax.comment"
        if token in Token.String:
            return "class:syntax.string"
        if token in Token.Keyword:
            return "class:syntax.keyword"
        if token in Token.Name.Builtin:
            return "class:syntax.builtin"
        if token in Token.Name.Function:
            return "class:syntax.function"
        if token in Token.Name.Class:
            return "class:syntax.class"
        if token in Token.Name.Decorator:
            return "class:syntax.decorator"
        if token in Token.Number:
            return "class:syntax.number"
        if token in Token.Operator:
            return "class:syntax.operator"
        return "class:code"

    return [(style_for(tok), text) for tok, text in lexer.get_tokens(code)]


class _ScrollableFormattedTextControl(FormattedTextControl):
    """A FormattedTextControl that *always* catches scroll-wheel events and
    forwards them to a callback. Used by the details pane so mouse wheel
    scrolling fires reliably regardless of which fragment is hovered.
    """

    def __init__(self, *args, on_scroll=None, on_event=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._on_scroll = on_scroll
        self._on_event = on_event  # called for *any* event — used for debug

    def mouse_handler(self, mouse_event: MouseEvent):  # type: ignore[override]
        if self._on_event is not None:
            try:
                self._on_event(mouse_event)
            except Exception:
                pass
        if self._on_scroll is not None:
            if mouse_event.event_type == MouseEventType.SCROLL_DOWN:
                self._on_scroll(+3)
                return None
            if mouse_event.event_type == MouseEventType.SCROLL_UP:
                self._on_scroll(-3)
                return None
        return super().mouse_handler(mouse_event)


def _section_header(label: str) -> list[tuple[str, str]]:
    """A compact, peach section label with a short trailing rule. Kept narrow
    so it never wraps inside the (now sidebar-narrowed) details pane."""
    return [
        ("class:section.label", f"{label.upper()}  "),
        ("class:rule", "─" * max(6, 28 - len(label))),
    ]


def _findings_list_cursor_row(findings: list[Finding], selected: int) -> int:
    """Rendered row containing ``selected`` in the variable-height list."""
    has_verification_badge = any(
        "sandbox" in (finding.source or "") or "browser" in (finding.source or "")
        for finding in findings
    )
    row = 2 + int(has_verification_badge)  # title, optional badge, blank
    for finding in findings[:max(0, selected)]:
        row += 2 + int(bool(finding.file_path))  # title, optional path, blank
    return row


def _highlight_code_by_lang(code: str, lang: str, fallback_file: str = "") -> list[tuple[str, str]]:
    """Tokenize code with Pygments using a language name; fall back to file-based detection."""
    try:
        from pygments.lexers import get_lexer_by_name
        from pygments.token import Token
        from pygments.util import ClassNotFound
    except ImportError:
        return [("class:code", code)]
    try:
        lexer = get_lexer_by_name(lang)
    except ClassNotFound:
        return _highlight_code(code, fallback_file)

    def style_for(tok):
        if tok in Token.Comment: return "class:syntax.comment"
        if tok in Token.String: return "class:syntax.string"
        if tok in Token.Keyword: return "class:syntax.keyword"
        if tok in Token.Name.Builtin: return "class:syntax.builtin"
        if tok in Token.Name.Function: return "class:syntax.function"
        if tok in Token.Name.Class: return "class:syntax.class"
        if tok in Token.Name.Decorator: return "class:syntax.decorator"
        if tok in Token.Number: return "class:syntax.number"
        if tok in Token.Operator: return "class:syntax.operator"
        return "class:code"

    return [(style_for(tok), t) for tok, t in lexer.get_tokens(code)]


# Inline markdown patterns: **bold**, *italic*, _italic_, `code`, [link](url)
_MD_INLINE_RE = __import__("re").compile(
    r"(\*\*[^*\n]+\*\*)"
    r"|(`[^`\n]+`)"
    r"|(\*(?!\s)[^*\n]+?\*)"
    r"|(_(?!\s)[^_\n]+?_)"
    r"|(\[[^\]\n]+\]\([^)\s]+\))"
)


def _render_md_inline(text: str) -> list[tuple[str, str]]:
    """Render inline markdown — bold/italic/code/links — into styled fragments."""
    import re as _re
    out: list[tuple[str, str]] = []
    pos = 0
    for m in _MD_INLINE_RE.finditer(text):
        if m.start() > pos:
            out.append(("", text[pos:m.start()]))
        token = m.group(0)
        if token.startswith("**") and token.endswith("**"):
            out.append(("class:md.bold", token[2:-2]))
        elif token.startswith("`") and token.endswith("`"):
            out.append(("class:md.code", token[1:-1]))
        elif token.startswith("*") and token.endswith("*"):
            out.append(("class:md.italic", token[1:-1]))
        elif token.startswith("_") and token.endswith("_"):
            out.append(("class:md.italic", token[1:-1]))
        elif token.startswith("["):
            link_m = _re.match(r"\[([^\]]+)\]\(([^)]+)\)", token)
            if link_m:
                out.append(("class:md.link", link_m.group(1)))
            else:
                out.append(("", token))
        pos = m.end()
    if pos < len(text):
        out.append(("", text[pos:]))
    return out


def _split_md_table_row(line: str) -> list[str]:
    """Split a GFM table row without treating escaped/code-span pipes as cells."""
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|") and not text.endswith(r"\|"):
        text = text[:-1]
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    in_code = False
    for char in text:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "`":
            in_code = not in_code
            current.append(char)
        elif char == "|" and not in_code:
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if escaped:
        current.append("\\")
    cells.append("".join(current).strip())
    return cells


def _is_md_table_divider(line: str) -> bool:
    import re as _re
    cells = _split_md_table_row(line)
    return bool(cells) and all(
        _re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in cells
    )


def _table_cell_text(cell: str) -> tuple[str, str]:
    """Return visible table-cell text and a useful whole-cell style."""
    import re as _re
    raw = cell.strip().replace("<br>", " / ").replace("<br/>", " / ")
    fragments = _render_md_inline(raw)
    text = "".join(part for _, part in fragments)
    style = ""
    if _re.fullmatch(r"`[^`\n]+`", raw):
        style = "class:md.code"
    elif _re.fullmatch(r"\*\*[^*\n]+\*\*", raw):
        style = "class:md.bold"
    elif _re.fullmatch(r"(?:\*[^*\n]+\*|_[^_\n]+_)", raw):
        style = "class:md.italic"
    return text, style


def _render_md_table(rows: list[list[str]], alignments: list[str]) -> list[tuple[str, str]]:
    """Render a GFM table as a bounded, wrapping terminal table."""
    import textwrap

    if not rows:
        return []
    columns = max(len(row) for row in rows)
    normalized = [row + [""] * (columns - len(row)) for row in rows]
    visible = [[_table_cell_text(cell) for cell in row] for row in normalized]

    # Wrap long cells within the table instead of letting prompt-toolkit split
    # a border halfway through a row.
    max_table_width = 88
    border_width = columns * 3 + 1
    available = max(columns * 3, max_table_width - border_width)
    minimums = [
        min(18, max(3, len(visible[0][index][0])))
        for index in range(columns)
    ]
    while sum(minimums) > available:
        widest = max(range(columns), key=lambda index: minimums[index])
        if minimums[widest] <= 3:
            break
        minimums[widest] -= 1
    widths = [
        min(
            40,
            max(minimums[index], *(len(row[index][0]) for row in visible)),
        )
        for index in range(columns)
    ]
    while sum(widths) > available:
        candidates = [
            index for index, width in enumerate(widths)
            if width > minimums[index]
        ]
        if not candidates:
            break
        widest = max(candidates, key=lambda index: widths[index] - minimums[index])
        widths[widest] -= 1

    out: list[tuple[str, str]] = []

    def border(left: str, middle: str, right: str) -> None:
        out.append(("class:md.table.border", left))
        for index, width in enumerate(widths):
            out.append(("class:md.table.border", "─" * (width + 2)))
            out.append((
                "class:md.table.border",
                right if index == columns - 1 else middle,
            ))
        out.append(("", "\n"))

    def rendered_row(row_index: int) -> None:
        wrapped: list[list[str]] = []
        for index, (text, _style) in enumerate(visible[row_index]):
            parts = textwrap.wrap(
                text,
                width=max(1, widths[index]),
                break_long_words=True,
                break_on_hyphens=False,
            )
            wrapped.append(parts or [""])
        height = max(len(parts) for parts in wrapped)
        for line_index in range(height):
            out.append(("class:md.table.border", "│"))
            for index in range(columns):
                text = (
                    wrapped[index][line_index]
                    if line_index < len(wrapped[index])
                    else ""
                )
                alignment = alignments[index] if index < len(alignments) else "left"
                if alignment == "right":
                    padded = text.rjust(widths[index])
                elif alignment == "center":
                    padded = text.center(widths[index])
                else:
                    padded = text.ljust(widths[index])
                style = (
                    "class:md.table.header"
                    if row_index == 0
                    else visible[row_index][index][1]
                )
                out.append(("", " "))
                out.append((style, padded))
                out.append(("", " "))
                out.append(("class:md.table.border", "│"))
            out.append(("", "\n"))

    border("┌", "┬", "┐")
    rendered_row(0)
    border("├", "┼", "┤")
    for row_index in range(1, len(rows)):
        rendered_row(row_index)
        if row_index < len(rows) - 1:
            border("├", "┼", "┤")
    border("└", "┴", "┘")
    return out


def _render_md_prose(text: str) -> list[tuple[str, str]]:
    """Render a chunk of markdown prose (no code fences) into styled fragments."""
    import re as _re
    out: list[tuple[str, str]] = []
    lines = text.split("\n")
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        if (
            idx + 1 < len(lines)
            and "|" in line
            and _is_md_table_divider(lines[idx + 1])
        ):
            raw_header = _split_md_table_row(line)
            divider = _split_md_table_row(lines[idx + 1])
            alignments = []
            for cell in divider:
                stripped = cell.strip()
                if stripped.startswith(":") and stripped.endswith(":"):
                    alignments.append("center")
                elif stripped.endswith(":"):
                    alignments.append("right")
                else:
                    alignments.append("left")
            table_rows = [raw_header]
            idx += 2
            while idx < len(lines) and "|" in lines[idx] and lines[idx].strip():
                table_rows.append(_split_md_table_row(lines[idx]))
                idx += 1
            out.extend(_render_md_table(table_rows, alignments))
            continue
        # ATX headers: #, ##, ###, ...
        m_h = _re.match(r"^(#{1,6})\s+(.*)$", line)
        if m_h:
            level = len(m_h.group(1))
            content = m_h.group(2).strip()
            style = (
                "class:md.h1" if level == 1
                else "class:md.h2" if level == 2
                else "class:md.h3"
            )
            out.append((style, content))
        # Horizontal rule
        elif _re.match(r"^[-*_]{3,}\s*$", line):
            out.append(("class:rule", "─" * 60))
        # Bullet list
        elif (m_b := _re.match(r"^(\s*)[-*+]\s+(.*)$", line)):
            out.append(("", m_b.group(1)))
            out.append(("class:md.bullet", "• "))
            out.extend(_render_md_inline(m_b.group(2)))
        # Numbered list
        elif (m_n := _re.match(r"^(\s*)(\d+)\.\s+(.*)$", line)):
            out.append(("", m_n.group(1)))
            out.append(("class:md.bullet", f"{m_n.group(2)}. "))
            out.extend(_render_md_inline(m_n.group(3)))
        # Blockquote
        elif (m_q := _re.match(r"^>\s?(.*)$", line)):
            out.append(("class:md.quote", "│ "))
            out.extend(_render_md_inline(m_q.group(1)))
        # Regular line
        else:
            out.extend(_render_md_inline(line))
        # Preserve newlines between lines
        if idx < len(lines) - 1:
            out.append(("", "\n"))
        idx += 1
    return out


def _render_markdown_with_code(text: str, default_file: str = "") -> list[tuple[str, str]]:
    """Render markdown text. Code fences are syntax-highlighted; prose handles
    headers, bold, italic, inline code, lists, blockquotes, and horizontal rules."""
    import re as _re
    if not text:
        return []
    fragments: list[tuple[str, str]] = []
    pattern = _re.compile(r"```([a-zA-Z0-9_+\-]*)\n(.*?)```", _re.DOTALL)
    last_end = 0
    for m in pattern.finditer(text):
        prose = text[last_end:m.start()]
        if prose:
            fragments.extend(_render_md_prose(prose))
        lang = m.group(1).strip()
        code = m.group(2)
        if lang:
            fragments.extend(_highlight_code_by_lang(code, lang, default_file))
        else:
            fragments.extend(_highlight_code(code, default_file))
        last_end = m.end()
    if last_end < len(text):
        fragments.extend(_render_md_prose(text[last_end:]))
    return fragments


def _short_tool_label(tool: str, args: dict) -> str:
    path = args.get("path", "")
    pattern = args.get("pattern", "")

    # ── Interactive agent tools: surface the actual command/target so the
    #    operator sees exactly what ran. ──────────────────────────────────
    def _clip(s: str, n: int = 110) -> str:
        # Collapse to ONE line: a heredoc/multi-line command would otherwise
        # spill raw source into the transcript.
        s = " ".join(str(s).split())
        return s if len(s) <= n else s[: n - 1] + "…"

    if tool == "run_command":
        return _clip(args.get("command", ""))
    if tool == "which":
        return _clip(args.get("tool", ""))
    if tool == "subdomains":
        return _clip(args.get("domain", ""))
    if tool in ("http_probe", "port_scan", "nuclei_scan"):
        target = args.get("target", "")
        extra = args.get("ports") or args.get("severity") or args.get("tags") or ""
        return _clip(f"{target}{('  ' + str(extra)) if extra else ''}".strip())
    if tool == "dns_lookup":
        return _clip(f"{args.get('name', '')} {args.get('record_type', 'A')}".strip())
    if tool in ("sca_scan", "secret_scan"):
        return _clip(args.get("path", "") or ".")
    if tool == "mailbox_new":
        return _clip(args.get("label", "") or "new address")
    if tool == "mailbox_wait":
        return _clip(f"wait {args.get('to', '')} /{args.get('match', '')}/".strip())
    if tool == "mailbox_list":
        return _clip(args.get("to", "") or "recent")
    if tool == "oob_register":
        return _clip(args.get("label", "") or "callback url")
    if tool == "oob_poll":
        return _clip(args.get("marker", ""))
    if tool == "browser_fetch":
        return _clip(args.get("url", ""))
    if tool == "web_search":
        return _clip(args.get("query", ""))
    if tool == "web_fetch":
        return _clip(args.get("url", ""))
    if tool == "bash_output":
        return _clip(args.get("shell_id", ""))
    if tool == "kill_shell":
        return _clip(args.get("shell_id", ""))
    if tool == "dispatch_specialist":
        return _clip(
            f"{args.get('vuln_class', '')} {args.get('target', '')}".strip()
        )
    if tool == "list_findings":
        return "list findings"
    # Paths are already relative to the project root (tools are rooted at
    # target_dir), so we surface them verbatim in the trace.
    if tool == "read_file" and path:
        return f"read {path}"
    if tool == "list_dir":
        return f"ls {path}" if path else "ls ."
    if tool == "glob" and pattern:
        scope = f" in {path}" if path else ""
        return f"glob {pattern}{scope}"
    if tool == "grep":
        p = pattern[:24] + "…" if len(pattern) > 24 else pattern
        scope = f" in {path}" if path else ""
        return f"grep /{p}/{scope}"
    if tool == "get_project_info":
        return "project info"
    if tool == "get_route_map":
        return "route map"
    if tool == "extract_functions" and path:
        return f"extract functions from {path}"
    if tool == "find_dangerous_patterns" and path:
        return f"find dangerous patterns in {path}"
    if tool == "trace_variable":
        var = args.get("variable_name", "")
        return f"trace {var} in {path}" if var else f"trace variable in {path}"
    if tool == "report_finding":
        cat = args.get("category", "")
        fp = args.get("file_path", "")
        if cat and fp:
            return f"report {cat} in {fp}"
        return f"report {cat}" if cat else "report finding"
    if tool == "validate_finding":
        return f"validate {args.get('status', '')}"
    if tool == "finish_hunt":
        return "finish hunt"
    if tool == "finish_validation":
        return "finish validation"
    if path:
        return f"{tool} {path}"
    return tool


def _summarize_tool_output(out) -> str:
    """One-line result summary for a tool call in the interactive transcript."""
    if not isinstance(out, dict):
        s = str(out).strip().replace("\n", " ")
        return (s[:120] + "…") if len(s) > 120 else s
    if "error" in out:
        return f"error: {str(out['error'])[:120]}"
    if "exit_code" in out:
        note = f"exit {out['exit_code']}"
        if out.get("timed_out"):
            note = "timed out"
        return note
    # web_fetch: report what actually came back, not nothing.
    if "status" in out and "text" in out:
        if out.get("blocked"):
            return f"blocked ({out['status']})"
        chars = len(out.get("text") or "")
        return f"{out['status']} · {chars:,} chars" if chars else f"{out['status']} · empty"
    for key, label in (
        ("count", "results"), ("interactions", "callbacks"),
        ("vulnerable_packages", "vulnerable packages"),
        ("files_scanned", "files scanned"), ("total_findings", "findings"),
    ):
        if key in out:
            return f"{out[key]} {label}"
    if "installed" in out:
        return "installed" if out["installed"] else "not installed"
    if "subdomains" in out:
        return f"{out.get('count', len(out['subdomains']))} subdomains"
    # Generic textual status — dispatch_specialist ("exploited"), a backgrounded
    # run_command ("running"), etc. Checked last so it can't shadow the richer
    # summaries above (notably web_fetch, whose `status` is an HTTP code).
    if isinstance(out.get("status"), str):
        extra = out.get("shell_id") or ""
        return f"{out['status']}{(' ' + extra) if extra else ''}"
    return ""


# ── App ───────────────────────────────────────────────────────────

class OpenHackApp:
    """Full-screen prompt_toolkit application driving the OpenHack TUI."""

    def __init__(self, resume_session_id: Optional[str] = None) -> None:
        self._resume_session_id = resume_session_id
        cfg = load_user_config()
        self.provider = resolve_provider(cfg.get("provider", settings.llm_provider))
        self.model = cfg.get("model") or PROVIDER_DEFAULTS.get(self.provider, settings.openhack_model_id)
        self.org_name: str = cfg.get("openhack_org_name") or ""
        self.user_email: str = ""  # populated lazily

        self.mode: str = "landing"  # "landing" | "scanning" | "viewing" | "sessions"
        self.previous_mode: Optional[str] = None  # set when entering "sessions" so Esc can return
        self.active_tab: str = "trace"  # "trace" | "findings" — sessions is its own mode now
        self.scan: Optional[ScanState] = None
        self.session: Optional[Session] = None
        self.scan_task: Optional[asyncio.Task] = None
        # Interactive-agent conversation state. When an agent session is open and
        # idle, follow-up input continues the same conversation (rather than being
        # queued into an already-finished loop).
        self.agent = None
        self.is_agent_session: bool = False
        # Live token stream: the in-progress agent message, rendered at the tail
        # of the transcript and cleared once the turn commits its trace line.
        self._stream_buf: str = ""
        self._stream_reasoning: str = ""
        # Bytes of tool-call arguments streamed so far this turn. Writing a
        # 20KB file is minutes of pure argument stream with no content and no
        # trace event, so without this the transcript sits silent and the run
        # looks hung (session cfeb868f).
        self._stream_tool_bytes: int = 0
        self._stream_last_invalidate: float = 0.0
        self.last_status_line: str = ""
        self.last_findings: list[Finding] = []  # findings from most recent scan
        self.last_session: Optional[Session] = None
        self.chat_history: list[Message] = []
        self._cancel_armed = False
        self._interrupting = False
        # Shell (bang) mode: the live foreground shell process + a flag for the
        # spinner verb, plus the shared background-shell manager (also used by
        # the agent's run_in_background tool).
        self._shell_proc = None
        self._shell_active = False
        # Transient upstream-retry notice from LLMClient (see _on_llm_status).
        self._llm_status = ""
        self.shells = ShellManager()
        self.shells_selected: int = 0
        self._shells_were_running = False  # ticker edge-detect for /bashes repaint
        # Sessions tab state
        self.sessions_index: list[dict] = []
        self.sessions_selected: int = 0
        self.model_index: list[dict] = []
        self.model_selected: int = 0
        self.provider_index: list[dict] = []
        self.provider_selected: int = 0
        self.viewing_target: str = ""  # header label when in "viewing" mode
        # Id of the report open in "viewing" mode — there is no live Session
        # then, so this is what keeps the bottom-right id honest.
        self.viewing_scan_id: str = ""
        # Findings tab selection (split pane: list left, details right)
        self.findings_selected: int = 0
        self.findings_list_hidden: bool = False  # toggle the left list via Ctrl+B / /sidebar
        # macOS terminals sometimes emit BOTH a mouse SCROLL event AND an
        # arrow-key event for a single trackpad gesture. Track when the last
        # mouse scroll happened so the arrow-key handler can stand down.
        self._last_scroll_at: float = 0.0
        # /logout uses a two-press confirmation; flag is reset on any other action.
        self._logout_armed: bool = False
        # /verify also uses two-press confirmation for the "enable" path so the
        # user reads the warning about prereqs. Stores which subject is armed.
        self._verify_arm_subject: Optional[str] = None  # 'sandbox' | 'browser' | 'all' | None
        # Mouse capture state. When True, prompt_toolkit consumes every mouse
        # event (so wheel-scroll + click-to-select work) but the terminal's
        # native drag-to-select-text is blocked. /mouse toggles this.
        self._mouse_enabled: bool = True
        # Centered modal-dialog state. None = no modal; otherwise a key
        # identifying which one to render (e.g. 'verify:sandbox', 'logout').
        self._modal_kind: Optional[str] = None
        self._modal_title: str = ""
        self._modal_body: str = ""
        self._modal_on_yes: Optional[Any] = None  # callable invoked on 'y' / Enter
        # Manual scroll offset for the details pane (in logical lines).
        # We bypass Window.vertical_scroll because prompt_toolkit's render
        # was clamping it back to 0 in our setup — instead we clip the
        # fragment list ourselves in details_text().
        self._details_scroll: int = 0
        # Findings sidebar width as a percentage of the Findings tab width.
        # The sibling Dimensions (built in _build_layout) hold weights that
        # we mutate to resize live.
        self._sidebar_pct: int = 35
        # Trace pane scroll. _trace_follow=True means stick to the bottom as
        # new events stream in; flipped off when the user scrolls up to read
        # history, flipped back on when they scroll back to bottom.
        self._trace_scroll: int = 0
        self._trace_follow: bool = True
        # Trace sidebar: 0 = "All", 1+ = scan.trace_agents[idx-1]
        self._trace_agent_idx: int = 0
        # Update/announcement state — populated asynchronously on startup.
        self._update_info: Optional[UpdateInfo] = None
        # Knight-rider spinner frame index, advanced by the run() ticker while
        # a scan is active. Wraps over _SPINNER_FRAMES.
        self._spin_idx: int = 0

        self.input_buffer = Buffer(
            multiline=False,
            completer=OpenHackCompleter(),
            complete_while_typing=True,
            accept_handler=self._on_buffer_accept,
        )

        self.kb = self._build_keybindings()
        self.layout = self._build_layout()
        self.style = self._build_style()

        # Kitty keyboard protocol: swap in a CSI-u-aware input so modifier
        # combos legacy encoding collapses (Option+Backspace, lone Escape,
        # Ctrl+key) arrive disambiguated on terminals that support it. Falls
        # back silently to the default input everywhere else. main() pushes the
        # protocol on the terminal when self.kitty_active is set.
        self._input = None
        self.kitty_active = False
        if settings.kitty_keyboard_protocol and sys.platform != "win32":
            try:
                if sys.stdin.isatty():
                    from openhack.kitty_keys import KittyVt100Input

                    self._input = KittyVt100Input(sys.stdin)
                    self.kitty_active = True
            except Exception:
                self._input = None
                self.kitty_active = False

        self.app: Application = Application(
            layout=self.layout,
            key_bindings=self.kb,
            style=self.style,
            input=self._input,
            full_screen=True,
            # Filter-driven so /mouse can toggle native copy on demand.
            # When False, the terminal's built-in drag-to-select works.
            mouse_support=Condition(lambda: self._mouse_enabled),
            erase_when_done=True,
            # The palette is full 24-bit; force truecolor so the hex theme
            # renders faithfully instead of being quantized to 256.
            color_depth=ColorDepth.DEPTH_24_BIT,
        )

        # Resume a saved session on startup (openhack --resume <id>): hydrate the
        # transcript and, for agent sessions, rebuild a continuable agent.
        if self._resume_session_id:
            try:
                self._resume_session(self._resume_session_id)
            except Exception as exc:
                self.last_status_line = f"could not resume session: {exc}"

    # ── Keybindings ───────────────────────────────────────────────

    def _build_keybindings(self) -> KeyBindings:
        kb = KeyBindings()

        @kb.add("c-c")
        def _ctrl_c(event):
            # Behavior:
            #   • Scan running, not paused → pause (state preserved)
            #   • Scan paused              → exit TUI (scan stays as 'running'
            #                                with dead PID → reclassified as
            #                                'aborted' next launch; resume with
            #                                'r' in /sessions)
            #   • No scan running          → exit TUI
            if self.mode == "scanning" and self.session is not None:
                if self.session.paused:
                    event.app.exit()
                else:
                    self.session.pause()
                    self.last_status_line = (
                        "scan paused · Ctrl+C again to exit · /resume to continue · /cancel to stop"
                    )
                    self._invalidate()
            else:
                event.app.exit()

        @kb.add("c-d")
        def _ctrl_d(event):
            if not self.input_buffer.text:
                event.app.exit()

        # Modal-dialog keys (eager so they take priority over text input
        # while a modal is open — typing 'y' goes to the dialog, not the
        # input box).
        modal_open = Condition(lambda: self._modal_kind is not None)

        @kb.add("y", filter=modal_open, eager=True)
        @kb.add("Y", filter=modal_open, eager=True)
        @kb.add("enter", filter=modal_open, eager=True)
        def _modal_yes(event):
            cb = self._modal_on_yes
            self._close_modal()
            if cb is not None:
                try:
                    cb()
                except Exception as exc:
                    self.last_status_line = f"action failed: {exc}"
            self._invalidate()

        @kb.add("n", filter=modal_open, eager=True)
        @kb.add("N", filter=modal_open, eager=True)
        @kb.add("escape", filter=modal_open, eager=True)
        def _modal_no(event):
            self._close_modal()
            self.last_status_line = "cancelled"
            self._invalidate()

        def _completion_open() -> bool:
            return self.input_buffer.complete_state is not None

        @kb.add("escape", eager=True, filter=Condition(_completion_open))
        def _escape_completion(event):
            event.current_buffer.cancel_completion()

        # ESC while a run is in flight — interrupt it (like Ctrl-C in Claude
        # Code). Eager so it fires on the first press, ahead of text input and
        # the word-select escape-sequences (those keep working when idle).
        is_running = Condition(
            lambda: (
                self.scan_task is not None
                and not self.scan_task.done()
                and self.mode == "scanning"
            )
        )

        @kb.add(
            "escape",
            eager=True,
            filter=is_running & ~modal_open & ~Condition(_completion_open),
        )
        def _escape_interrupt(event):
            self._interrupt_run()

        # ESC while idle — clear a half-typed line (non-eager so the two-key
        # Option+Shift+arrow word-select sequences still resolve).
        @kb.add("escape", eager=False, filter=~Condition(_completion_open))
        def _escape(event):
            buf = event.current_buffer
            if buf.text:
                buf.reset()

        # Option+Shift+Left/Right — select word (macOS sends Escape + ShiftLeft/Right)
        @kb.add("escape", "s-left")
        def _select_word_left(event):
            buf = event.current_buffer
            pos = buf.document.find_previous_word_beginning() or 0
            buf.cursor_position += pos
            buf.start_selection()
            # Already moved — selection is from new pos to old pos
            # Re-do: move back, start selection, then move
            buf.cursor_position -= pos
            buf.start_selection()
            buf.cursor_position += pos

        @kb.add("escape", "s-right")
        def _select_word_right(event):
            buf = event.current_buffer
            pos = buf.document.find_next_word_ending() or 0
            buf.start_selection()
            buf.cursor_position += pos

        # Option/Meta + Backspace — erase the word before the cursor (the
        # standard word-delete every shell/editor does). macOS sends ESC + DEL
        # (\x1b\x7f) which prompt_toolkit parses as (escape, c-h); the ESC+BS and
        # Option+forward-delete variants land on (escape, delete). We bind them
        # explicitly and eagerly so the behavior is deterministic and can't be
        # pre-empted by the lone-escape handlers.
        @kb.add("escape", "backspace", eager=True, filter=~modal_open)
        @kb.add("escape", "delete", eager=True, filter=~modal_open)
        @kb.add("c-w", eager=True, filter=~modal_open)
        def _delete_word_before(event):
            buf = event.current_buffer
            pos = buf.document.find_previous_word_beginning()
            if pos:
                buf.delete_before_cursor(-pos)
            elif buf.document.text_before_cursor:
                buf.delete_before_cursor(len(buf.document.text_before_cursor))

        # Tab navigation — only in scanning/viewing modes, and only when the
        # input is empty so they don't conflict with typing.
        def _in_tabs() -> bool:
            return self.mode in ("scanning", "viewing")

        def _in_sessions() -> bool:
            return self.mode == "sessions"

        def _in_models() -> bool:
            return self.mode == "models"

        def _in_providers() -> bool:
            return self.mode == "providers"

        def _input_empty() -> bool:
            return not self.input_buffer.text

        @kb.add("c-t")
        def _ctrl_t(event):
            if _in_tabs():
                self._cycle_tab(+1)

        @kb.add("right", filter=Condition(lambda: _in_tabs() and _input_empty()))
        def _right(event):
            self._cycle_tab(+1)

        @kb.add("left", filter=Condition(lambda: _in_tabs() and _input_empty()))
        def _left(event):
            self._cycle_tab(-1)

        for i, name in enumerate(("trace", "findings"), 1):
            @kb.add(str(i), filter=Condition(lambda: _in_tabs() and _input_empty()))
            def _digit(event, _name=name):
                self.active_tab = _name
                self._invalidate()

        # Findings list navigation (Findings tab + empty input).
        def _on_findings() -> bool:
            return _in_tabs() and self.active_tab == "findings" and _input_empty()

        def _reset_details_scroll() -> None:
            self._details_scroll = 0

        def _move_selection(delta: int) -> None:
            self._move_finding_selection(delta)

        def _scroll_details(delta: int) -> None:
            self._details_scroll = max(0, self._details_scroll + delta)
            self._invalidate()

        # Up / Down switch findings (keyboard nav). [ and ] are aliases.
        @kb.add("up", filter=Condition(_on_findings))
        def _f_up(event):
            _move_selection(-1)

        @kb.add("down", filter=Condition(_on_findings))
        def _f_down(event):
            _move_selection(+1)

        @kb.add("[", filter=Condition(_on_findings))
        def _f_prev(event):
            _move_selection(-1)

        @kb.add("]", filter=Condition(_on_findings))
        def _f_next(event):
            _move_selection(+1)

        # 'y' = yank the finding as an AI-agent prompt to the system clipboard.
        @kb.add("y", filter=Condition(_on_findings))
        def _f_yank(event):
            self._cmd_copy_fix()

        # < / > resize the sidebar / details split by 5% steps.
        def _resize_sidebar(delta_pct: int) -> None:
            self._sidebar_pct = max(15, min(75, self._sidebar_pct + delta_pct))
            self._sidebar_dim.weight = self._sidebar_pct
            self._details_dim.weight = 100 - self._sidebar_pct
            self.last_status_line = f"sidebar {self._sidebar_pct}% · details {100 - self._sidebar_pct}%"
            self._invalidate()

        @kb.add("<", filter=Condition(_on_findings))
        def _f_shrink(event):
            _resize_sidebar(-5)

        @kb.add(">", filter=Condition(_on_findings))
        def _f_grow(event):
            _resize_sidebar(+5)

        # Trace tab scrolling — keyboard fallbacks for the mouse wheel.
        def _on_trace() -> bool:
            return _in_tabs() and self.active_tab == "trace" and _input_empty()

        # Up / Down → navigate the trace sidebar (agent picker).
        # PgUp / PgDn → scroll the trace content (was Up/Down before).
        def _move_trace_agent(delta: int) -> None:
            if self.scan is None:
                return
            # Total entries = "All" + however many agents the tree shows.
            # Compute by counting trace_agents (which is what the tree is built
            # from) — works for both the flat sidebar and the tree variant.
            n_entries = 1 + len(self.scan.trace_agents)
            if n_entries <= 1:
                return
            self._trace_agent_idx = max(0, min(n_entries - 1, self._trace_agent_idx + delta))
            self._trace_scroll = 0
            self._trace_follow = True
            self._invalidate()

        # Up / Down scroll the transcript. (They used to cycle a per-agent
        # "picker" — removed; there's no agent list in a conversation.)
        @kb.add("up", filter=Condition(_on_trace))
        def _t_up(event):
            self._scroll_trace_by(-3)

        @kb.add("down", filter=Condition(_on_trace))
        def _t_down(event):
            self._scroll_trace_by(+3)

        # PgUp / PgDn scroll the trace content by a page.
        @kb.add("pageup", filter=Condition(_on_trace))
        def _t_pgup(event):
            self._scroll_trace_by(-12)

        @kb.add("pagedown", filter=Condition(_on_trace))
        def _t_pgdn(event):
            self._scroll_trace_by(+12)

        @kb.add("home", filter=Condition(_on_trace))
        def _t_home(event):
            # Home: jump to "All" + reset trace to top.
            self._trace_agent_idx = 0
            self._trace_follow = False
            self._trace_scroll = 0
            self._invalidate()

        @kb.add("end", filter=Condition(_on_trace))
        def _t_end(event):
            # End: stay on current agent filter, jump trace to bottom.
            self._trace_follow = True
            self._invalidate()

        # Mouse wheel over the right pane is the primary scroll mechanism.
        # Keyboard fallbacks below work when the input box is empty so they
        # don't conflict with typing.
        @kb.add("pageup", filter=Condition(_on_findings))
        def _details_pgup(event):
            _scroll_details(-12)

        @kb.add("pagedown", filter=Condition(_on_findings))
        def _details_pgdn(event):
            _scroll_details(+12)

        @kb.add("home", filter=Condition(_on_findings))
        def _f_home(event):
            _reset_details_scroll()
            self._invalidate()

        @kb.add("end", filter=Condition(_on_findings))
        def _f_end(event):
            _scroll_details(+10_000)

        # Ctrl+B toggles the contextual sidebar: the right session panel on
        # Trace and the findings list on Findings.
        @kb.add("c-b", filter=Condition(lambda: _in_tabs() and _input_empty()))
        def _toggle_sidebar(event):
            self._toggle_sidebar_visibility()

        # Sessions overlay keybindings.
        @kb.add("up", filter=Condition(lambda: _in_sessions() and _input_empty()))
        def _up(event):
            if self.sessions_index:
                self.sessions_selected = max(0, self.sessions_selected - 1)
                self._invalidate()

        @kb.add("down", filter=Condition(lambda: _in_sessions() and _input_empty()))
        def _down(event):
            if self.sessions_index:
                self.sessions_selected = min(len(self.sessions_index) - 1, self.sessions_selected + 1)
                self._invalidate()

        @kb.add("enter", filter=Condition(lambda: _in_sessions() and _input_empty()))
        def _enter(event):
            self._load_selected_session()

        @kb.add("r", filter=Condition(lambda: _in_sessions() and _input_empty()))
        def _resume(event):
            self._resume_selected_session()

        @kb.add("escape", eager=True, filter=Condition(lambda: _in_sessions() and _input_empty()))
        def _esc_sessions(event):
            self._close_sessions_overlay()

        # Model picker keybindings.
        @kb.add("up", filter=Condition(lambda: _in_models() and _input_empty()))
        def _m_up(event):
            if self.model_index:
                self.model_selected = max(0, self.model_selected - 1)
                self._invalidate()

        @kb.add("down", filter=Condition(lambda: _in_models() and _input_empty()))
        def _m_down(event):
            if self.model_index:
                self.model_selected = min(len(self.model_index) - 1, self.model_selected + 1)
                self._invalidate()

        @kb.add("enter", filter=Condition(lambda: _in_models() and _input_empty()))
        def _m_enter(event):
            self._select_model_from_picker()

        @kb.add("escape", eager=True, filter=Condition(lambda: _in_models() and _input_empty()))
        def _m_esc(event):
            self._close_model_picker()

        # Provider picker keybindings.
        @kb.add("up", filter=Condition(lambda: _in_providers() and _input_empty()))
        def _p_up(event):
            if self.provider_index:
                self.provider_selected = max(0, self.provider_selected - 1)
                self._invalidate()

        @kb.add("down", filter=Condition(lambda: _in_providers() and _input_empty()))
        def _p_down(event):
            if self.provider_index:
                self.provider_selected = min(
                    len(self.provider_index) - 1, self.provider_selected + 1
                )
                self._invalidate()

        @kb.add("enter", filter=Condition(lambda: _in_providers() and _input_empty()))
        def _p_enter(event):
            self._select_provider_from_picker()

        @kb.add("escape", eager=True, filter=Condition(lambda: _in_providers() and _input_empty()))
        def _p_esc(event):
            self._close_provider_picker()

        # Background-shell watcher (/bashes).
        def _in_shells() -> bool:
            return self.mode == "shells"

        @kb.add("up", filter=Condition(lambda: _in_shells() and _input_empty()))
        def _sh_up(event):
            self.shells_selected = max(0, self.shells_selected - 1)
            self._invalidate()

        @kb.add("down", filter=Condition(lambda: _in_shells() and _input_empty()))
        def _sh_down(event):
            n = len(self.shells.list())
            self.shells_selected = min(max(0, n - 1), self.shells_selected + 1)
            self._invalidate()

        @kb.add("k", filter=Condition(lambda: _in_shells() and _input_empty()))
        def _sh_kill(event):
            shells = self.shells.list()
            if shells:
                sel = max(0, min(self.shells_selected, len(shells) - 1))
                self.shells.kill(shells[sel].id)
                self.last_status_line = f"killed {shells[sel].id}"
                self._invalidate()

        @kb.add("escape", eager=True, filter=Condition(lambda: _in_shells() and _input_empty()))
        def _sh_esc(event):
            self.mode = self.previous_mode or "landing"
            self.previous_mode = None
            self._invalidate()

        return kb

    def _cycle_tab(self, direction: int) -> None:
        order = ["trace", "findings"]
        try:
            idx = order.index(self.active_tab)
        except ValueError:
            idx = 0
        self.active_tab = order[(idx + direction) % len(order)]
        self._invalidate()

    # ── Style ─────────────────────────────────────────────────────

    def _build_style(self) -> Style:
        # All colors drawn from the dark palette (see OH_* constants).
        return Style.from_dict({
            # canvas
            "body": f"bg:{OH_BG} {OH_TEXT}",
            # logo / wordmark
            "logo": f"bold {OH_TEXT}",
            "logo.dim": OH_MUTED,
            "logo.bright": f"bold {OH_TEXT}",
            "logo.mark": f"bold {OH_PRIMARY}",  # signal-green ground symbol
            "wordmark": f"bold {OH_TEXT}",
            "tagline": OH_MUTED,
            # tip line
            "tip": OH_MUTED,
            "tip.label": f"bold {OH_ORANGE}",
            "tip.key": OH_TEXT,
            "footer": OH_MUTED,
            "footer.bright": f"bold {OH_TEXT}",
            "footer.dot": OH_GREEN,
            # input box (signal-green left bar, element bg)
            "input.box": f"bg:{OH_ELEM}",
            "input.bar": f"{OH_SECONDARY} bg:{OH_ELEM}",
            "input.prompt": OH_SECONDARY,
            "input.placeholder": f"bg:{OH_ELEM} {OH_MUTED}",
            "input.model.agent": f"bg:{OH_ELEM} {OH_SECONDARY}",
            "input.model.sep": f"bg:{OH_ELEM} {OH_MUTED}",
            "input.model.name": f"bg:{OH_ELEM} {OH_TEXT}",
            "input.model.provider": f"bg:{OH_ELEM} {OH_MUTED}",
            # hints
            "hint": OH_MUTED,
            "hint.key": OH_TEXT,
            "keybar": OH_MUTED,
            "keybar.key": OH_TEXT,
            "keybar.sep": OH_BORDER,
            "rule": OH_BORDER_SUB,
            # spinner / status row
            "spinner": OH_SECONDARY,
            "spinner.dim": OH_BORDER,
            "status.working": OH_TEXT,
            "status.esc": OH_TEXT,
            "status.esc.label": OH_MUTED,
            "status.usage": OH_MUTED,
            # header
            "header.brand": f"bold {OH_PRIMARY}",
            "header.brandname": f"bold {OH_TEXT}",
            "header.sep": OH_MUTED,
            "header.target": f"bold {OH_TEXT}",
            "header.meta": OH_MUTED,
            # sidebar
            "sidebar": f"bg:{OH_PANEL} {OH_TEXT}",
            "sidebar.header": f"bg:{OH_PANEL} bold {OH_TEXT}",
            "sidebar.label": f"bg:{OH_PANEL} {OH_MUTED}",
            "sidebar.value": f"bg:{OH_PANEL} {OH_TEXT}",
            "sidebar.dot": f"bg:{OH_PANEL} {OH_GREEN}",
            "sidebar.path.dim": f"bg:{OH_PANEL} {OH_MUTED}",
            "sidebar.path.bright": f"bg:{OH_PANEL} bold {OH_TEXT}",
            "sidebar.sep": f"bg:{OH_PANEL} {OH_BORDER_SUB}",
            # tabs
            "tab.active": f"bold {OH_PRIMARY}",
            "tab.inactive": OH_MUTED,
            "tab.key": OH_TEXT,
            # panes
            "pane.title": f"bold {OH_TEXT}",
            "pane.empty": OH_MUTED,
            "pane.dim": OH_MUTED,
            "verified": f"bold {OH_GREEN}",
            "status.pending": OH_MUTED,
            "status.running": f"bold {OH_SECONDARY}",
            "status.working": f"bold {OH_ORANGE}",
            "status.done": f"bold {OH_GREEN}",
            "status.fail": f"bold {OH_RED}",
            "agent.name": OH_TEXT,
            "agent.detail": OH_MUTED,
            # severity
            "sev.critical": f"bold {OH_RED}",
            "sev.high": f"bold {OH_RED}",
            "sev.medium": f"bold {OH_ORANGE}",
            "sev.low": f"bold {OH_CYAN}",
            "sev.info": OH_MUTED,
            "finding.title": OH_TEXT,
            "finding.path": OH_MUTED,
            "finding.cursor": f"bold {OH_PRIMARY}",
            # trace / message stream
            "trace.time": OH_MUTED,
            "trace.agent": OH_SECONDARY,
            "trace.arrow": OH_MUTED,
            "trace.tool": f"bold {OH_TEXT}",
            "trace.dim": OH_MUTED,
            "trace.step": f"bold {OH_PRIMARY}",
            # User messages get a full-width grey band + a neutral white bar, so
            # they're instantly distinct from the agent's green-barred output.
            "trace.user": f"bg:{OH_USER_BG} {OH_TEXT}",
            "trace.user.bar": f"bg:{OH_USER_BG} bold {OH_TEXT}",
            "trace.agent.bar": f"bold {OH_PRIMARY}",
            "trace.stream": OH_TEXT,
            # Tool rows are secondary to the conversation: keep them neutral so
            # green stays reserved for the agent's own voice (the ▌ bar). Bold
            # gives the tool name hierarchy over its args without adding colour.
            "trace.tool.name": f"bold {OH_MUTED}",
            "trace.shell": OH_TEXT,
            # A `!cmd` is the user's own command — neutral, like their messages.
            "trace.shell.cmd": f"bg:{OH_USER_BG} bold {OH_TEXT}",
            "trace.tool.dot": OH_BORDER_A,  # the → marking a tool call (recedes)
            "trace.fail": OH_RED,           # folded-in error outcome
            "msg.bar": OH_SECONDARY,
            "msg.bar.error": OH_RED,
            "msg.meta.glyph": OH_PRIMARY,
            "msg.meta.agent": OH_TEXT,
            "msg.meta.dim": OH_MUTED,
            # sessions
            "session.row": OH_TEXT,
            "session.row.selected": f"bold {OH_PRIMARY}",
            "session.meta": OH_MUTED,
            # sections / code / markdown
            "section.label": f"bold {OH_PRIMARY}",
            "section.box": OH_PRIMARY,
            "code": OH_TEXT,
            "md.h1": f"bold {OH_ACCENT} underline",
            "md.h2": f"bold {OH_ACCENT}",
            "md.h3": f"bold {OH_TEXT}",
            "md.bold": f"bold {OH_ORANGE}",
            "md.italic": f"italic {OH_YELLOW}",
            "md.code": f"bg:{OH_ELEM} {OH_GREEN}",
            "md.bullet": OH_PRIMARY,
            "md.link": f"underline {OH_CYAN}",
            "md.quote": f"italic {OH_YELLOW}",
            "md.table.border": OH_BORDER,
            "md.table.header": f"bold {OH_TEXT}",
            # syntax
            "syntax.comment": f"italic {OH_MUTED}",
            "syntax.string": OH_GREEN,
            "syntax.keyword": f"bold {OH_ACCENT}",
            "syntax.builtin": OH_CYAN,
            "syntax.function": OH_PRIMARY,
            "syntax.class": f"bold {OH_YELLOW}",
            "syntax.decorator": OH_YELLOW,
            "syntax.number": OH_ORANGE,
            "syntax.operator": OH_CYAN,
            "log": OH_MUTED,
            # modal
            "modal.frame": f"bg:{OH_PANEL} {OH_TEXT}",
            "modal.title": f"bg:{OH_PANEL} bold {OH_PRIMARY}",
            "modal.body": f"bg:{OH_PANEL} {OH_TEXT}",
            "modal.hint": f"bg:{OH_PANEL} {OH_MUTED}",
            "modal.key": f"bg:{OH_PANEL} bold {OH_PRIMARY}",
            "completion-menu.completion": f"bg:{OH_ELEM} {OH_TEXT}",
            "completion-menu.completion.current": f"bg:{OH_SECONDARY} {OH_BG}",
        })

    # ── Layout ────────────────────────────────────────────────────

    def _build_layout(self) -> Layout:
        is_landing = Condition(lambda: self.mode == "landing")
        is_sessions = Condition(lambda: self.mode == "sessions")
        is_models = Condition(lambda: self.mode == "models")
        is_providers = Condition(lambda: self.mode == "providers")
        is_shells = Condition(lambda: self.mode == "shells")
        is_scanning = Condition(lambda: self.mode in ("scanning", "viewing"))

        landing = self._build_landing_container()
        scan = self._build_scan_container()
        sessions = self._build_sessions_container()
        models = self._build_model_container()
        providers = self._build_provider_container()
        shells = self._build_shells_container()

        body = HSplit([
            ConditionalContainer(content=landing, filter=is_landing),
            ConditionalContainer(content=sessions, filter=is_sessions),
            ConditionalContainer(content=models, filter=is_models),
            ConditionalContainer(content=providers, filter=is_providers),
            ConditionalContainer(content=shells, filter=is_shells),
            ConditionalContainer(content=scan, filter=is_scanning),
        ])

        # ── Modal overlay: centered Frame shown when self._modal_kind set ──
        def _modal_text():
            return [
                ("class:modal.title", self._modal_title),
                ("", "\n\n"),
                ("class:modal.body", self._modal_body),
                ("", "\n\n"),
                ("class:modal.key", "[Y]"),
                ("class:modal.hint", " confirm   "),
                ("class:modal.key", "[N]"),
                ("class:modal.hint", " cancel   "),
                ("class:modal.key", "[Esc]"),
                ("class:modal.hint", " dismiss"),
            ]

        modal_body_window = Window(
            FormattedTextControl(_modal_text),
            wrap_lines=True,
            style="class:modal.frame",
        )
        modal_frame = Frame(
            body=modal_body_window,
            title="OpenHack",
            style="class:modal.frame",
            width=D(min=0, max=80, preferred=72),
            height=D(min=0, max=20, preferred=14),
        )
        # Center via weight-1 spacers on all four sides inside the full-screen Float.
        modal_centered = HSplit([
            Window(height=D(weight=1)),
            VSplit([
                Window(width=D(weight=1)),
                modal_frame,
                Window(width=D(weight=1)),
            ]),
            Window(height=D(weight=1)),
        ])
        modal_visible = Condition(lambda: self._modal_kind is not None)

        root = FloatContainer(
            content=body,
            floats=[
                Float(
                    xcursor=True,
                    ycursor=True,
                    content=CompletionsMenu(max_height=12, scroll_offset=1),
                ),
                Float(
                    top=0, left=0, right=0, bottom=0,
                    content=ConditionalContainer(modal_centered, filter=modal_visible),
                ),
            ],
        )
        return Layout(root, focused_element=self._input_window)

    # ── Shared input/footer components ────────────────────────────

    def _wordmark_lines(self) -> list[list[tuple[str, str]]]:
        """The 'OpenHack' wordmark as a single line of bold brand-tone text."""
        return [[("class:wordmark", _WORDMARK)]]

    def _placeholder_text(self) -> str:
        return "Ask anything · /scan to scan · !cmd to run a shell command"

    def _current_session_id(self) -> str:
        """Short id of the session on screen, or "" when there isn't one.

        Prefers the live session, falling back to a report being viewed, so the
        id shown always belongs to what the transcript is actually displaying.
        It is the same id `_write_report` keys the file by, which is what makes
        it valid for `openhack --resume`.
        """
        sid = ""
        if self.session is not None:
            sid = self.session.id or ""
        if not sid:
            sid = self.viewing_scan_id or ""
        return sid[:8]

    def _model_line(self) -> list[tuple[str, str]]:
        """The '<cwd> · <model> <provider>' line under the input.

        The cwd is read live (not the scan's frozen target) so /cd is reflected
        immediately — and it lives here, at the bottom, because the TUI has no
        top chrome.
        """
        cwd = _abbrev_home(os.getcwd())
        if len(cwd) > 44:  # keep the line readable — show the tail that matters
            parts = cwd.split(os.sep)
            cwd = "…" + os.sep + os.sep.join(parts[-2:]) if len(parts) > 2 else cwd[-44:]
        return [
            ("class:input.box", "  "),
            ("class:input.model.agent", cwd),
            ("class:input.model.sep", " · "),
            ("class:input.model.name", self.model or "grok-4.5"),
            ("class:input.model.provider", f"  {self.provider}"),
        ]

    def _make_input_window(self) -> Window:
        """Create the shared buffer window (blue prompt, dim placeholder)."""
        return Window(
            content=BufferControl(
                buffer=self.input_buffer,
                input_processors=[
                    BeforeInput("  ", style="class:input.box"),
                    _PlaceholderProcessor(lambda: "  " + self._placeholder_text()),
                ],
            ),
            height=1,
            style="class:input.box",
        )

    def _input_box(self, width) -> VSplit:
        """The prompt: a signal-green left accent bar + element-bg box holding
        the input line and the model/agent status line."""
        inner = HSplit([
            Window(height=1, style="class:input.box"),  # airy top padding
            self._input_window,
            Window(height=1, style="class:input.box"),  # gap before the model line
            Window(
                FormattedTextControl(self._model_line),
                height=1, style="class:input.box",
            ),
            Window(height=1, style="class:input.box"),  # bottom padding
        ], style="class:input.box")
        return VSplit([
            Window(width=1, char="▌", style="class:input.bar"),
            inner,
            Window(width=1, style="class:input.box"),
        ], width=width, style="class:input.box")

    def _cwd_fragments(self, dim: str, bright: str) -> list[tuple[str, str]]:
        """`~/parent/`(dim) + `name`(bright) + `:branch`(dim)."""
        cwd = os.getcwd()
        disp = _abbrev_home(cwd)
        branch = _git_branch(cwd)
        parent, _, name = disp.rpartition("/")
        out: list[tuple[str, str]] = []
        if parent:
            out.append((dim, parent + "/"))
        out.append((bright, name or disp))
        if branch:
            out.append((dim, ":" + branch))
        return out

    def _build_landing_container(self) -> HSplit:
        # Landing: the green ground mark over the two-tone wordmark, a brand
        # tagline, a bordered prompt with the signal-green accent bar, a
        # right-aligned shortcut row, a tip line, and a cwd/version footer.
        mark_windows = [
            Window(
                FormattedTextControl(lambda row=row: [("class:logo.mark", row)]),
                align=WindowAlign.CENTER, height=1, style="class:body",
            )
            for row in _MARK_ROWS
        ]
        wm = self._wordmark_lines()
        logo_windows = [
            Window(
                FormattedTextControl(lambda frags=frags: frags),
                align=WindowAlign.CENTER, height=1, style="class:body",
            )
            for frags in wm
        ]

        def tagline():
            return [("class:tagline", "The open-source security agent")]

        def tip():
            cfg = load_user_config()
            logged_in = bool(
                cfg.get("openhack_user_first_name")
                or cfg.get("openhack_user_email")
                or self.user_email
            )
            if not logged_in:
                return [
                    ("class:tip.label", "● Tip  "),
                    ("class:tip", "Run "),
                    ("class:tip.key", "/login"),
                    ("class:tip", " to get $20 in free credits and start scanning"),
                ]
            return [
                ("class:tip.label", "● Tip  "),
                ("class:tip", "Type "),
                ("class:tip.key", "/scan ."),
                ("class:tip", " to scan the current directory, or "),
                ("class:tip.key", "?"),
                ("class:tip", " for help"),
            ]

        def hints():
            return [
                ("class:hint.key", "tab"), ("class:hint", " complete    "),
                ("class:hint.key", "enter"), ("class:hint", " submit    "),
                ("class:hint.key", "?"), ("class:hint", " help"),
            ]

        def version_right():
            return [("class:footer", f"OpenHack {OPENHACK_VERSION}"), ("class:footer", "  ")]

        self._input_window = self._make_input_window()
        # Cap max == preferred so leftover width flows to the side spacers and
        # the box stays centered (rather than stretching to fill the row).
        box_width = D(min=0, preferred=80, max=80)

        # The prompt box + right-aligned shortcut row, centered horizontally.
        box_region = VSplit([
            Window(width=D(weight=1), style="class:body"),
            HSplit([
                self._input_box(box_width),
                Window(height=1, style="class:body"),
                Window(FormattedTextControl(hints), height=1,
                       align=WindowAlign.RIGHT, style="class:body"),
            ], width=box_width),
            Window(width=D(weight=1), style="class:body"),
        ], style="class:body")

        footer = VSplit([
            Window(width=1, style="class:body"),
            Window(
                FormattedTextControl(
                    lambda: self._cwd_fragments("class:footer", "class:footer.bright")
                ),
                height=1, style="class:body",
            ),
            Window(FormattedTextControl(version_right), height=1,
                   align=WindowAlign.RIGHT, style="class:body"),
        ], style="class:body")

        return HSplit([
            Window(height=D(weight=1), style="class:body"),  # top spacer
            *mark_windows,
            Window(height=1, style="class:body"),
            *logo_windows,
            Window(height=1, style="class:body"),
            Window(FormattedTextControl(tagline), align=WindowAlign.CENTER,
                   height=1, style="class:body"),
            Window(height=2, style="class:body"),
            box_region,
            Window(height=1, style="class:body"),
            Window(FormattedTextControl(tip), align=WindowAlign.CENTER,
                   height=1, style="class:body"),
            Window(height=1, style="class:body"),
            Window(
                FormattedTextControl(self._update_banner_text),
                align=WindowAlign.CENTER, wrap_lines=True, style="class:body",
            ),
            Window(
                FormattedTextControl(lambda: [
                    ("class:log", f"  {self.last_status_line}" if self.last_status_line else "")
                ]),
                wrap_lines=True, align=WindowAlign.CENTER, style="class:body",
            ),
            Window(height=D(weight=1), style="class:body"),  # bottom spacer
            footer,
            Window(height=1, style="class:body"),
        ], style="class:body")

    @staticmethod
    def _clip_tool_row(fragments: list, width: int) -> list:
        """Keep a tool row on ONE line at any terminal width: truncate the
        command in the middle, but always preserve the trailing result (the
        `· exit 0` / `· error: …` the operator actually needs)."""
        total = sum(len(text) for _, text in fragments)
        if total <= width:
            return fragments
        head, tail = list(fragments), []
        # The folded-in outcome is the final fragment when present.
        if head and head[-1][1].lstrip().startswith("·"):
            tail = [head.pop()]
        budget = max(10, width - sum(len(t) for _, t in tail) - 1)
        out, used = [], 0
        for style, text in head:
            if used >= budget:
                break
            if len(text) <= budget - used:
                out.append((style, text))
                used += len(text)
            else:
                out.append((style, text[: budget - used]))
                used = budget
                break
        out.append(("class:trace.dim", "…"))
        out.extend(tail)
        return out

    @staticmethod
    def _user_band_pad(fragments: list, width: int) -> int:
        """Spaces needed to extend a user message's grey band to the end of the
        row (and to fill the last row when it wraps). 0 for non-user lines."""
        if not any(str(style).startswith("class:trace.user") for style, _ in fragments):
            return 0
        used = sum(len(text) for _, text in fragments)
        return (-used) % max(1, width)

    def _trace_pane_width(self, default: int = 80) -> int:
        """Current width of the transcript pane — used to pad user messages into
        a full-width band. Falls back sanely before the first render."""
        try:
            info = self._trace_window.render_info if hasattr(self, "_trace_window") else None
            if info is not None and info.window_width:
                return max(20, int(info.window_width))
        except Exception:
            pass
        return default

    def _build_scan_container(self) -> HSplit:
        # No header bar: the TUI deliberately has no top chrome. Target/cwd,
        # model, run state, findings + cost and shortcuts all live at the bottom
        # (see _model_line, spinner_frags, usage_frags, keybar).

        # ── Tab bar ───────────────────────────────────────────────
        def tab_bar():
            findings = self._current_findings()
            count = len(findings)
            tabs = [("trace", "Trace"), ("findings", f"Findings ({count})")]
            out: list[tuple[str, str]] = [("", "  ")]
            for i, (key, label) in enumerate(tabs, 1):
                active = self.active_tab == key
                cls = "class:tab.active" if active else "class:tab.inactive"
                out.append(("class:tab.key", f" {i} "))
                out.append((cls, f" {label} "))
                out.append(("", "  "))
            # Shortcuts moved to the bottom keybar to keep the top uncluttered.
            return out

        tab_bar_window = Window(FormattedTextControl(tab_bar), height=1)

        # ── Trace tab ─────────────────────────────────────────────
        def _agent_tree() -> list[tuple[int, str]]:
            """Flatten scan.trace_agents into [(indent_level, agent_name), …].

            Swarms like 'hunter_swarm' adopt their 'hunter:*' children as
            level-1 entries underneath. Other agents stay at level 0.
            """
            if self.scan is None:
                return []
            agents = self.scan.trace_agents
            agent_set = set(agents)
            # Map of parent_swarm_name -> [child names in original order]
            children_map: dict[str, list[str]] = {}
            for a in agents:
                if ":" in a:
                    base = a.split(":", 1)[0]
                    parent = f"{base}_swarm"
                    if parent in agent_set:
                        children_map.setdefault(parent, []).append(a)

            seen: set[str] = set()
            out: list[tuple[int, str]] = []
            for a in agents:
                if a in seen:
                    continue
                if ":" in a:
                    base = a.split(":", 1)[0]
                    parent = f"{base}_swarm"
                    if parent in agent_set:
                        # Will be emitted under its parent when parent is visited.
                        continue
                # Top-level entry.
                out.append((0, a))
                seen.add(a)
                # Children (if a is a known swarm parent).
                for c in children_map.get(a, []):
                    if c not in seen:
                        out.append((1, c))
                        seen.add(c)
            # Orphans — any agent not yet emitted (parent wasn't actually seen).
            for a in agents:
                if a not in seen:
                    out.append((0, a))
                    seen.add(a)
            return out

        def _selected_trace_agents() -> Optional[set[str]]:
            """None = show all events; otherwise a set of agent names to include.

            Selecting a swarm parent expands to include all its children, so
            'hunter_swarm' shows events from hunter_swarm AND every hunter:*.
            """
            if self.scan is None:
                return None
            idx = self._trace_agent_idx
            if idx <= 0:
                return None
            tree = _agent_tree()
            if not tree:
                return None
            idx = min(idx - 1, len(tree) - 1)
            _, name = tree[idx]
            if name.endswith("_swarm"):
                base = name[: -len("_swarm")]
                return {name} | {
                    a for a in self.scan.trace_agents
                    if a.startswith(f"{base}:")
                }
            return {name}

        def _trace_text_raw():
            # Show the live tail whenever the agent turn is in flight — even
            # before any token streams — so waiting shows an animated spinner.
            running = bool(
                self.is_agent_session and self.scan is not None
                and self.scan.end_time is None
                and self.scan_task is not None and not self.scan_task.done()
            )
            streaming = bool(self._stream_buf or self._stream_reasoning or running)
            if self.scan is None or not self.scan.trace_lines:
                if streaming:
                    return self._stream_line()
                return [("class:pane.empty", "  no trace yet — start a scan with /scan <path>")]
            wanted = _selected_trace_agents()
            out: list[tuple[str, str]] = []
            matched = 0
            width = self._trace_pane_width()
            for agent, fragments in self.scan.trace_lines:
                if wanted is not None and agent not in wanted:
                    continue
                # Tool rows stay on one line at any width (command clipped,
                # outcome preserved); prose and user messages wrap normally.
                if any(style == "class:trace.tool.dot" for style, _ in fragments):
                    fragments = self._clip_tool_row(fragments, width)
                for fragment in fragments:
                    out.append(fragment)
                # User messages render as a full-width band: pad with styled
                # spaces up to the next multiple of the pane width so the
                # background covers the whole row (and every wrapped row).
                pad = self._user_band_pad(fragments, width)
                if pad:
                    out.append(("class:trace.user", " " * pad))
                out.append(("", "\n"))
                matched += 1
            if matched == 0 and wanted is not None:
                label = next(iter(wanted)) if len(wanted) == 1 else f"{len(wanted)} agents"
                return [("class:pane.empty", f"  no events from {label} (yet)")]
            # Append the in-progress agent message as it streams in.
            if streaming:
                out.append(("", "\n"))
                out.extend(self._stream_line())
            return out

        def trace_text():
            """Manual viewport clipping. _trace_follow=True sticks to the
            bottom; otherwise show from _trace_scroll."""
            raw = _trace_text_raw()
            try:
                lines = list(split_lines(raw))
            except Exception:
                return raw
            if not lines:
                return raw
            info = self._trace_window.render_info if hasattr(self, '_trace_window') else None
            window_height = info.window_height if info is not None else 20
            max_scroll = max(0, len(lines) - window_height)
            if self._trace_follow:
                self._trace_scroll = max_scroll
            elif self._trace_scroll > max_scroll:
                self._trace_scroll = max_scroll
            visible = lines[self._trace_scroll:]
            out: list[tuple[str, str]] = []
            for i, line in enumerate(visible):
                out.extend(line)
                if i < len(visible) - 1:
                    out.append(("", "\n"))
            return out

        def _scroll_trace_by(delta: int) -> None:
            # If user scrolls up, break the auto-follow.
            if delta < 0:
                self._trace_follow = False
            # Bump the offset, then re-clamp on next render.
            self._trace_scroll = max(0, self._trace_scroll + delta)
            # If we scrolled down past the visible content end, re-enable follow.
            if delta > 0 and self.scan and self.scan.trace_lines:
                # Each entry is (agent, fragments); a rendered line is 1 + the
                # newlines inside its fragment texts.
                total_lines = sum(
                    sum(frag[1].count("\n") for frag in fragments if len(frag) > 1) + 1
                    for _agent, fragments in self.scan.trace_lines
                )
                info = self._trace_window.render_info if hasattr(self, '_trace_window') else None
                window_height = info.window_height if info is not None else 20
                if self._trace_scroll >= max(0, total_lines - window_height):
                    self._trace_follow = True
            self._invalidate()

        trace_window = Window(
            content=_ScrollableFormattedTextControl(
                text=trace_text,
                focusable=False,
                on_scroll=_scroll_trace_by,
            ),
            # Wrap so full relative paths in tool calls stay visible. Manual
            # scroll counts logical (\n-delimited) lines, not visual rows, so
            # wrap doesn't break the scroll offset.
            wrap_lines=True,
            always_hide_cursor=True,
        )
        self._trace_window = trace_window
        self._scroll_trace_by = _scroll_trace_by

        # ── Trace sidebar: tree of agents that have produced events. ──
        # Sidebar entries (flat list, ordered):
        #   index 0 = "All"
        #   index 1..N = _agent_tree() entries (level 0 or 1 with indent)
        def trace_sidebar_text():
            tree = _agent_tree() if self.scan is not None else []
            n_entries = 1 + len(tree)  # "All" + tree
            out: list = [("class:pane.title", "  agents\n\n")]
            if len(tree) == 0:
                # "All" + waiting message.
                def _handler_all(event: MouseEvent):
                    if event.event_type == MouseEventType.MOUSE_UP:
                        self._trace_agent_idx = 0
                        self._invalidate()
                cls0 = "class:finding.cursor" if self._trace_agent_idx == 0 else "class:trace.agent"
                pointer0 = "❯ " if self._trace_agent_idx == 0 else "  "
                out.append((cls0, f"  {pointer0}All", _handler_all))
                out.append(("", "\n", _handler_all))
                out.append(("class:pane.empty", "\n  (waiting for events)\n"))
                return out

            def _make_handler(idx: int):
                def _handler(event: MouseEvent):
                    if event.event_type == MouseEventType.MOUSE_UP:
                        self._trace_agent_idx = idx
                        self._trace_scroll = 0
                        self._trace_follow = True
                        self._invalidate()
                return _handler

            # Clamp selection if agents list shrank since last selection.
            if self._trace_agent_idx >= n_entries:
                self._trace_agent_idx = 0

            # Entry 0: "All"
            sel = self._trace_agent_idx == 0
            cls = "class:finding.cursor" if sel else "class:trace.agent"
            pointer = "❯ " if sel else "  "
            h0 = _make_handler(0)
            out.append((cls, f"  {pointer}All", h0))
            out.append(("", "\n", h0))

            # Tree entries
            for i, (level, name) in enumerate(tree, start=1):
                sel = i == self._trace_agent_idx
                handler = _make_handler(i)
                pointer = "❯ " if sel else "  "
                if level == 0:
                    indent = ""
                    label_full = name
                    cls = "class:finding.cursor" if sel else "class:trace.agent"
                else:
                    indent = "  ├─ "
                    label_full = name
                    cls = "class:finding.cursor" if sel else "class:trace.dim"
                shown = label_full if len(label_full) <= 24 else label_full[:23] + "…"
                out.append((cls, f"  {pointer}{indent}{shown}", handler))
                out.append(("", "\n", handler))
            return out

        def _trace_sidebar_cursor() -> Point:
            # Row 0 = title ("agents"), row 1 = blank, row 2+ = entries.
            # Each entry is 1 row. Selected index maps to row (2 + idx).
            return Point(x=0, y=2 + self._trace_agent_idx)

        trace_sidebar_ctrl = FormattedTextControl(
            trace_sidebar_text, focusable=False,
            get_cursor_position=_trace_sidebar_cursor,
        )
        trace_sidebar = Window(
            content=trace_sidebar_ctrl,
            wrap_lines=False,
            always_hide_cursor=True,
            width=D(weight=25, preferred=10_000),
        )
        trace_sep = Window(
            FormattedTextControl(lambda: [("class:rule", "│\n") for _ in range(0, 200)]),
            width=1,
        )
        # Keep the trace full-width. The old permanent agent-filter column
        # duplicated information already visible in the trace and consumed a
        # quarter of the terminal. Agent attribution remains on each scan row.
        trace_pane = VSplit([
            VSplit([
                Window(width=1),
                trace_window,
            ], width=D(weight=1, preferred=10_000)),
        ])

        # ── Findings tab (split: list on left, details on right) ──
        def findings_list_text():
            findings = self._current_findings()
            count = len(findings)
            # Per-finding verification summary: how many have been confirmed by
            # the sandbox / browser verifier. Source is a comma-joined string
            # like "sandbox,browser" — split and bucket.
            sb_n = sum(1 for f in findings if "sandbox" in (f.source or ""))
            br_n = sum(1 for f in findings if "browser" in (f.source or ""))
            out: list[tuple[str, str, "MouseEvent"] | tuple[str, str]] = [
                ("class:pane.title", f"  Findings ({count})\n"),
            ]
            if sb_n or br_n:
                badge_parts: list[str] = []
                if sb_n:
                    badge_parts.append(f"sandbox ✓ {sb_n}/{count}")
                if br_n:
                    badge_parts.append(f"browser ✓ {br_n}/{count}")
                out.append(("class:pane.dim", f"  {' · '.join(badge_parts)}\n"))
            out.append(("", "\n"))
            if not findings:
                out.append(("class:pane.empty", "  none yet — start a scan with /scan <path>\n"))
                return out

            def _make_handler(idx: int):
                def _handler(event: MouseEvent):
                    if event.event_type == MouseEventType.MOUSE_UP:
                        self.findings_selected = idx
                        self._details_scroll = 0
                        self._invalidate()
                return _handler

            for i, f in enumerate(findings):
                selected = i == self.findings_selected
                handler = _make_handler(i)
                pointer = "❯ " if selected else "  "
                row_cls = "class:finding.cursor" if selected else "class:finding.title"
                # Verified badge: green ✓ when sandbox- or browser-validated.
                src = f.source or ""
                if "sandbox" in src and "browser" in src:
                    verified_mark = ("class:verified", "✓✓ ")
                elif "sandbox" in src or "browser" in src:
                    verified_mark = ("class:verified", "✓  ")
                else:
                    verified_mark = ("", "   ")
                # The row itself — clickable
                out.append((row_cls, f"  {pointer}", handler))
                out.append((verified_mark[0], verified_mark[1], handler))
                out.append((_sev_style(f.severity), f" {_sev_label(f.severity)} ", handler))
                out.append(("", "  ", handler))
                # Truncate the title to keep the list pane scannable.
                title = f.title if len(f.title) <= 60 else f.title[:57] + "…"
                out.append((row_cls, title, handler))
                out.append(("", "\n", handler))
                if f.file_path:
                    short_path = f.file_path
                    if len(short_path) > 64:
                        short_path = "…" + short_path[-63:]
                    out.append(("class:finding.path", f"          {short_path}\n", handler))
                out.append(("", "\n", handler))
            return out

        def _findings_cursor() -> Point:
            findings = self._current_findings()
            if not findings:
                return Point(x=0, y=0)
            selected = max(0, min(len(findings) - 1, self.findings_selected))
            return Point(
                x=0,
                y=_findings_list_cursor_row(findings, selected),
            )

        def _scroll_findings(delta: int) -> None:
            self._move_finding_selection(delta, from_mouse=True)

        # ── Details pane: a single scrollable Window ──
        def _selected_finding():
            findings = self._current_findings()
            if not findings:
                return None
            if self.findings_selected >= len(findings):
                self.findings_selected = max(0, len(findings) - 1)
            return findings[self.findings_selected]

        def _scroll_details_by(delta: int) -> None:
            self._details_scroll = max(0, self._details_scroll + delta)
            self._last_scroll_at = time.monotonic()
            self._invalidate()

        def _details_text_raw():
            f = _selected_finding()
            if f is None:
                return [("class:pane.empty", "  no findings to inspect")]
            out: list[tuple[str, str]] = []

            out.append(("class:pane.title", f"{f.title}\n"))
            out.append(("", "\n"))
            out.append((_sev_style(f.severity), f" {_sev_label(f.severity)} "))
            if f.category:
                out.append(("", "  "))
                out.append(("class:finding.path", f.category))
            if getattr(f, "cvss_score", None):
                out.append(("", "  "))
                out.append(("class:trace.dim", f"CVSS {f.cvss_score:.1f}"))
            src = f.source or ""
            verifiers = [v for v in ("sandbox", "browser") if v in src]
            if verifiers:
                out.append(("", "  "))
                out.append(("class:verified", "✓ verified via " + ", ".join(verifiers)))
            out.append(("", "\n"))
            if f.file_path:
                loc = f.file_path
                if getattr(f, "line_number", None):
                    loc += f":{f.line_number}"
                out.append(("class:finding.path", f"{loc}\n"))
            out.append(("", "\n"))

            if f.description:
                out.extend(_section_header("Description"))
                out.append(("", "\n"))
                out.append(("", f.description))
                out.append(("", "\n\n"))

            snippet = getattr(f, "code_snippet", None)
            if snippet:
                out.extend(_section_header("Vulnerable code"))
                out.append(("", "\n"))
                out.extend(_highlight_code(snippet, f.file_path or ""))
                out.append(("", "\n\n"))

            fix = getattr(f, "fix", None)
            if fix:
                out.extend(_section_header("Recommended fix"))
                out.append(("", "\n"))
                out.extend(_render_markdown_with_code(fix, f.file_path or ""))
                out.append(("", "\n\n"))
            else:
                out.append(("class:trace.dim", "No fix saved for this finding.\n"))

            return out

        def details_text():
            """Manual viewport-clipping scroll: drop the first N logical
            lines from the rendered fragments based on self._details_scroll."""
            raw = _details_text_raw()
            try:
                lines = list(split_lines(raw))
            except Exception:
                return raw
            if not lines:
                return raw
            # Clamp scroll so that the last line lands at the bottom of the
            # viewport — no scrolling past the end into blank space.
            info = self._details_window.render_info
            window_height = info.window_height if info is not None else 20
            max_scroll = max(0, len(lines) - window_height)
            if self._details_scroll > max_scroll:
                self._details_scroll = max_scroll
            visible = lines[self._details_scroll:]
            out: list[tuple[str, str]] = []
            for i, line in enumerate(visible):
                out.extend(line)
                if i < len(visible) - 1:
                    out.append(("", "\n"))
            return out

        # The custom control catches SCROLL_UP/SCROLL_DOWN at the control
        # level — guaranteed to fire on wheel events anywhere over this
        # Window, regardless of which fragment is under the cursor.
        details_window = Window(
            content=_ScrollableFormattedTextControl(
                text=details_text,
                focusable=False,
                on_scroll=_scroll_details_by,
            ),
            wrap_lines=True,
            always_hide_cursor=True,
        )
        self._details_window = details_window

        # Resizable split: the two Dimensions are stored on self so the
        # < / > keybindings can mutate their `weight` to change the ratio.
        self._sidebar_dim = D(weight=self._sidebar_pct, preferred=10_000)
        self._details_dim = D(weight=100 - self._sidebar_pct, preferred=10_000)

        # Findings list pane (left)
        findings_list_pane = Window(
            content=_ScrollableFormattedTextControl(
                text=findings_list_text,
                focusable=False,
                get_cursor_position=_findings_cursor,
                on_scroll=_scroll_findings,
            ),
            wrap_lines=False,
            always_hide_cursor=True,
            width=self._sidebar_dim,
        )
        details_sep = Window(
            FormattedTextControl(lambda: [("class:rule", "│\n") for _ in range(0, 200)]),
            width=1,
        )
        # Right pane — symmetric horizontal padding so content sits balanced.
        findings_details_pane = VSplit([
            Window(width=2),
            details_window,
            Window(width=2),
        ], width=self._details_dim)
        sidebar_visible = Condition(lambda: not self.findings_list_hidden)
        findings_pane = VSplit([
            ConditionalContainer(findings_list_pane, filter=sidebar_visible),
            ConditionalContainer(details_sep, filter=sidebar_visible),
            findings_details_pane,
        ])

        # ── Body: one of the two tabs ─────────────────────────────
        body = HSplit([
            ConditionalContainer(content=trace_pane,
                                 filter=Condition(lambda: self.active_tab == "trace")),
            ConditionalContainer(content=findings_pane,
                                 filter=Condition(lambda: self.active_tab == "findings")),
        ])

        # ── Right sidebar (session panel) ─────────────────────────
        P = "  "  # left padding inside the sidebar

        def sidebar_body():
            out: list[tuple[str, str]] = []
            # Session header
            if self.mode == "viewing":
                title = "Viewing session"
            elif self.scan is not None and self.scan.end_time is not None:
                title = "Scan complete"
            elif self.scan is not None:
                title = "Scanning…"
            else:
                title = "New session"
            out.append(("class:sidebar.header", f"{P}{title}\n"))
            target = ""
            if self.scan is not None:
                target = self._short_target(self.scan.target or "")
            if self.mode == "viewing":
                target = self._short_target(self.viewing_target or target)
            if target:
                out.append(("class:sidebar.label", f"{P}{target}\n"))
            out.append(("class:sidebar", "\n"))

            # Context: elapsed + spend.
            out.append(("class:sidebar.header", f"{P}Context\n"))
            elapsed = self.scan.elapsed_str() if self.scan is not None else "0:00"
            cost = self.scan.cost if self.scan is not None else 0.0
            out.append(("class:sidebar.value", f"{P}{elapsed}"))
            out.append(("class:sidebar.label", " elapsed\n"))
            out.append(("class:sidebar.value", f"{P}${cost:.2f}"))
            out.append(("class:sidebar.label", " spent\n"))
            out.append(("class:sidebar", "\n"))

            # Findings: severity breakdown.
            findings = self._current_findings()
            out.append(("class:sidebar.header", f"{P}Findings\n"))
            if not findings:
                out.append(("class:sidebar.label", f"{P}none yet\n"))
            else:
                counts: dict[str, int] = {}
                for f in findings:
                    counts[(f.severity or "info").lower()] = counts.get(
                        (f.severity or "info").lower(), 0) + 1
                for sev, label in (("critical", "Critical"), ("high", "High"),
                                   ("medium", "Medium"), ("low", "Low"),
                                   ("info", "Info")):
                    c = counts.get(sev, 0)
                    if not c:
                        continue
                    out.append((_sev_style(sev), f"{P}● "))
                    out.append(("class:sidebar.value", f"{c} "))
                    out.append(("class:sidebar.label", f"{label}\n"))
            out.append(("class:sidebar", "\n"))

            # Activity: the latest event line.
            out.append(("class:sidebar.header", f"{P}Activity\n"))
            msg = (self.scan.last_message if self.scan is not None else "") or "idle"
            if len(msg) > 34:
                msg = msg[:33] + "…"
            out.append(("class:sidebar.label", f"{P}{msg}\n"))
            return out

        def sidebar_footer():
            out: list[tuple[str, str]] = [("class:sidebar", f"{P}")]
            out.extend(self._cwd_fragments("class:sidebar.path.dim",
                                           "class:sidebar.path.bright"))
            out.append(("class:sidebar", "\n"))
            out.append(("class:sidebar.dot", f"{P}● "))
            out.append(("class:sidebar.path.bright", "OpenHack "))
            out.append(("class:sidebar.label", OPENHACK_VERSION))
            return out

        sidebar = HSplit([
            Window(FormattedTextControl(sidebar_body), wrap_lines=True,
                   style="class:sidebar", always_hide_cursor=True),
            Window(FormattedTextControl(sidebar_footer), height=2,
                   style="class:sidebar", always_hide_cursor=True),
            Window(height=1, style="class:sidebar"),
        ], width=D(min=0, preferred=42), style="class:sidebar")

        sidebar_divider = Window(width=1, char="│", style="class:sidebar.sep")
        # The scan sidebar (Session/Context/Findings/Activity) is meaningful for a
        # real scan pipeline. In an interactive agent conversation it's just noise
        # ("Scan complete", "none yet") — findings count + cost already live in the
        # bottom bar — so hide it there and let the chat use the full width.
        sidebar_pane_visible = Condition(
            lambda: not self.findings_list_hidden and not self.is_agent_session
        )

        # ── Main split: body (left, flexible) + sidebar (right) ───
        main = VSplit([
            body,
            ConditionalContainer(sidebar_divider, filter=sidebar_pane_visible),
            ConditionalContainer(sidebar, filter=sidebar_pane_visible),
        ])

        # ── Bottom status row (spinner + esc | usage + hint) ──────
        def spinner_frags():
            running = (self.scan is not None and self.scan.end_time is None
                       and self.mode == "scanning")
            if running:
                frame = _SPINNER_FRAMES[self._spin_idx % len(_SPINNER_FRAMES)]
                elapsed = self.scan.elapsed_str() if self.scan else ""
                out: list[tuple[str, str]] = [
                    ("class:spinner", f"  {frame}  "),
                ]
                out.extend(_shimmer_fragments(self._processing_verb()))
                if elapsed:
                    out.append(("class:spinner.dim", f"  {elapsed}"))
                # An upstream retry/backoff, so a long wait reads as "retrying"
                # rather than a frozen app. Cleared as soon as the call recovers.
                if self._llm_status:
                    out.append(("class:spinner.dim", "   ·   "))
                    out.append(("class:sev.medium", self._llm_status))
                out.append(("class:spinner.dim", "   ·   "))
                out.append(("class:status.esc", "esc"))
                out.append(("class:status.esc.label", " interrupt"))
                return out
            msg = self.last_status_line or (self.scan.last_message if self.scan else "")
            return [("class:log", f"  {msg}" if msg else "")]

        def usage_frags():
            parts: list[tuple[str, str]] = []
            # Run state used to sit in the (now removed) top bar.
            label = ""
            if self.mode == "viewing":
                label = "viewing"
            elif self.session is not None and self.session.paused:
                label = "⏸ paused"
            elif self.scan is not None and self.scan.end_time is not None:
                label = "complete"
            # Findings + cost only for a scan. ScanState.cost is fed by
            # step_complete / swarm_complete, which only the scan pipeline
            # emits — in an agent conversation it can never be anything but
            # $0.00 with 0 findings, so showing it there is just a lie. The
            # real per-turn cost lands in the status line when a turn ends,
            # and /cost reports the session total.
            usage = ""
            if self.scan is not None and not self.is_agent_session:
                n = len(self._current_findings())
                usage = f"{n} findings  ·  ${self.scan.cost:.2f}"
            if label:
                parts.append(("class:header.meta", label))
            if label and usage:
                parts.append(("class:header.meta", "  ·  "))
            if usage:
                parts.append(("class:status.usage", usage))
            # The session id, always visible while a session exists — so the
            # scan you're watching is identifiable without digging through
            # ~/.openhack/scans. Short form on purpose: it matches what
            # /sessions lists, and `--resume` globs on a prefix, so this is
            # enough to reopen the run.
            sid = self._current_session_id()
            if sid:
                if parts:
                    parts.append(("class:header.meta", "  ·  "))
                parts.append(("class:keybar.sep", "scan "))
                parts.append(("class:header.meta", sid))
            parts.append(("", "  "))
            return parts

        bottom_status = VSplit([
            Window(FormattedTextControl(spinner_frags), height=1, style="class:body"),
            Window(FormattedTextControl(usage_frags), height=1,
                   align=WindowAlign.RIGHT, style="class:body"),
        ], height=1, style="class:body")

        # ── Keybar: shortcuts live here at the bottom, not crammed by the tabs.
        def keybar_frags():
            # Context-aware: a conversation doesn't need the findings/resize keys.
            if self.is_agent_session:
                items = [
                    ("⏎", "send"), ("↑↓", "scroll"),
                    ("/clear", "reset"), ("?", "help"),
                ]
            else:
                items = [
                    ("←/→", "tab"), ("↑↓", "scroll"), ("[ ]", "finding"),
                    ("< >", "resize"), ("Ctrl+B", "sidebar"),
                    ("/sessions", "past scans"), ("?", "help"),
                ]
            out: list[tuple[str, str]] = [("", "  ")]
            for i, (key, label) in enumerate(items):
                if i:
                    out.append(("class:keybar.sep", " · "))
                out.append(("class:keybar.key", key))
                out.append(("class:keybar", f" {label}"))
            return out

        keybar = Window(FormattedTextControl(keybar_frags), height=1, style="class:body")

        # The Trace/Findings tab bar is scan furniture. In an agent conversation
        # there's just the transcript (findings open on demand via /findings), so
        # hide it there.
        tabs_visible = Condition(lambda: not self.is_agent_session)
        return HSplit([
            Window(height=1, style="class:body"),  # top padding
            # No top chrome: brand/target/state all live at the bottom now, so
            # the transcript starts at the top of the screen (Claude-Code style).
            ConditionalContainer(tab_bar_window, filter=tabs_visible),
            main,
            Window(height=1, style="class:body"),
            bottom_status,
            self._input_box(D(weight=1)),
            Window(height=1, style="class:body"),
            keybar,
            Window(height=1, style="class:body"),
        ], style="class:body")

    def _build_sessions_container(self) -> HSplit:
        """Standalone sessions overlay — full-screen picker, no tab bar."""
        def header_text():
            return [
                ("class:header.brand", "openhack"),
                ("class:header.sep", "  ·  "),
                ("class:header.target", "sessions"),
                ("class:header.sep", "    "),
                ("class:header.meta",
                 f"{len(self.sessions_index)} saved scan(s)" if self.sessions_index else "no saved scans"),
            ]

        def sessions_text():
            out: list[tuple[str, str]] = [("", "\n")]
            if not self.sessions_index:
                out.append((
                    "class:pane.empty",
                    "  no saved scans yet — completed scans are saved to ~/.openhack/scans/\n",
                ))
                return out
            for i, row in enumerate(self.sessions_index):
                selected = i == self.sessions_selected
                cls = "class:session.row.selected" if selected else "class:session.row"
                pointer = "❯ " if selected else "  "
                out.append((cls, f"  {pointer}{row.get('label', '')}"))
                out.append(("", "\n"))
                out.append(("class:session.meta", f"      {row.get('meta', '')}"))
                out.append(("", "\n\n"))
            return out

        def hint_text():
            return [
                ("class:hint", "  ↑/↓ "),
                ("class:hint.key", "navigate"),
                ("class:hint", "   enter "),
                ("class:hint.key", "load"),
                ("class:hint", "   esc "),
                ("class:hint.key", "back"),
            ]

        header = Window(FormattedTextControl(header_text), height=1)
        rule = Window(FormattedTextControl(lambda: [("class:rule", "─" * 240)]), height=1)
        def _sessions_cursor() -> Point:
            # Row 0 = leading blank. Each session = 3 rows (label, meta, blank).
            return Point(x=0, y=1 + self.sessions_selected * 3)

        body = Window(
            FormattedTextControl(
                sessions_text, focusable=False,
                get_cursor_position=_sessions_cursor,
            ),
            wrap_lines=False,
            always_hide_cursor=True,
        )
        hint = Window(FormattedTextControl(hint_text), height=1)

        return HSplit([
            Window(height=1),
            header,
            rule,
            body,
            rule,
            hint,
            VSplit([Window(width=2), self._input_window]),
            Window(height=1),
        ])

    def _build_model_container(self) -> HSplit:
        """Standalone model picker — full-screen, scroll with ↑/↓, enter selects."""
        def header_text():
            return [
                ("class:header.brand", "openhack"),
                ("class:header.sep", "  ·  "),
                ("class:header.target", "model"),
                ("class:header.sep", "    "),
                ("class:header.meta", f"{len(self.model_index)} available"),
            ]

        def models_text():
            out: list[tuple[str, str]] = [("", "\n")]
            if not self.model_index:
                out.append(("class:pane.empty", "  no models available\n"))
                return out
            for i, m in enumerate(self.model_index):
                selected = i == self.model_selected
                active = m["id"] == self.model
                cls = "class:session.row.selected" if selected else "class:session.row"
                pointer = "❯ " if selected else "  "
                mark = "  ●" if active else "   "
                out.append((cls, f"  {pointer}{m['label']}"))
                out.append(("class:session.meta", f"    {m['id']}"))
                out.append(("class:sev.low" if active else "class:session.meta", f"{mark}"))
                out.append(("", "\n"))
                if m.get("desc"):
                    out.append(("class:session.meta", f"      {m['desc']}"))
                out.append(("", "\n\n"))
            return out

        def hint_text():
            return [
                ("class:hint", "  ↑/↓ "),
                ("class:hint.key", "navigate"),
                ("class:hint", "   enter "),
                ("class:hint.key", "select"),
                ("class:hint", "   esc "),
                ("class:hint.key", "cancel"),
            ]

        header = Window(FormattedTextControl(header_text), height=1)
        rule = Window(FormattedTextControl(lambda: [("class:rule", "─" * 240)]), height=1)

        def _models_cursor() -> Point:
            # Row 0 = leading blank. Each model = 3 rows (label, desc, blank).
            return Point(x=0, y=1 + self.model_selected * 3)

        body = Window(
            FormattedTextControl(
                models_text, focusable=False,
                get_cursor_position=_models_cursor,
            ),
            wrap_lines=False,
            always_hide_cursor=True,
        )
        hint = Window(FormattedTextControl(hint_text), height=1)

        return HSplit([
            Window(height=1),
            header,
            rule,
            body,
            rule,
            hint,
            VSplit([Window(width=2), self._input_window]),
            Window(height=1),
        ])

    def _build_provider_container(self) -> HSplit:
        """Scrollable provider switcher backed by Models.dev."""
        def header_text():
            return [
                ("class:header.brand", "openhack"),
                ("class:header.sep", "  ·  "),
                ("class:header.target", "provider"),
                ("class:header.sep", "    "),
                ("class:header.meta", f"{len(self.provider_index)} available"),
            ]

        def providers_text():
            out: list[tuple[str, str]] = [("", "\n")]
            if not self.provider_index:
                return [("class:pane.empty", "  no providers available\n")]
            for i, provider in enumerate(self.provider_index):
                selected = i == self.provider_selected
                active = provider["id"] == self.provider
                cls = "class:session.row.selected" if selected else "class:session.row"
                pointer = "❯ " if selected else "  "
                mark = "●" if active else " "
                connection = "connected" if provider["connected"] else "connect required"
                out.append((cls, f"  {pointer}{provider['label']}"))
                out.append(
                    (
                        "class:sev.low" if active else "class:session.meta",
                        f"  {mark}\n",
                    )
                )
                out.append(
                    (
                        "class:session.meta",
                        f"      {provider['id']} · {connection}",
                    )
                )
                if provider.get("hint"):
                    out.append(("class:session.meta", f" · {provider['hint']}"))
                out.append(("", "\n\n"))
            return out

        def hint_text():
            return [
                ("class:hint", "  ↑/↓ "),
                ("class:hint.key", "navigate"),
                ("class:hint", "   enter "),
                ("class:hint.key", "switch"),
                ("class:hint", "   /connect <provider> "),
                ("class:hint.key", "authenticate"),
                ("class:hint", "   esc "),
                ("class:hint.key", "cancel"),
            ]

        def cursor() -> Point:
            return Point(x=0, y=1 + self.provider_selected * 3)

        header = Window(FormattedTextControl(header_text), height=1)
        rule = Window(
            FormattedTextControl(lambda: [("class:rule", "─" * 240)]), height=1
        )
        body = Window(
            FormattedTextControl(
                providers_text, focusable=False, get_cursor_position=cursor
            ),
            wrap_lines=False,
            always_hide_cursor=True,
        )
        hint = Window(FormattedTextControl(hint_text), height=1)
        return HSplit(
            [
                Window(height=1),
                header,
                rule,
                body,
                rule,
                hint,
                VSplit([Window(width=2), self._input_window]),
                Window(height=1),
            ]
        )

    def _build_shells_container(self) -> HSplit:
        """Background-shell watcher — the shell list plus the selected one's tail."""
        def header_text():
            shells = self.shells.list()
            running = sum(1 for s in shells if s.is_running())
            return [
                ("class:header.brand", "openhack"),
                ("class:header.sep", "  ·  "),
                ("class:header.target", "shells"),
                ("class:header.sep", "    "),
                ("class:header.meta", f"{running} running · {len(shells)} total"),
            ]

        def _status_style(s):
            if s.status == "running":
                return "class:sev.low"
            if s.status == "killed":
                return "class:status.fail"
            return "class:trace.dim" if s.returncode == 0 else "class:sev.medium"

        def shells_text():
            out: list[tuple[str, str]] = [("", "\n")]
            shells = self.shells.list()
            if not shells:
                out.append(("class:pane.empty",
                            "  no background shells — start one with  !<command> &\n"))
                return out
            sel = max(0, min(self.shells_selected, len(shells) - 1))
            for i, s in enumerate(shells):
                selected = i == sel
                cls = "class:session.row.selected" if selected else "class:session.row"
                pointer = "❯ " if selected else "  "
                badge = {
                    "running": "● running",
                    "exited": f"exited {s.returncode}",
                    "killed": "killed",
                }.get(s.status, s.status)
                out.append((cls, f"  {pointer}{s.id}  "))
                out.append((_status_style(s), badge))
                out.append(("class:session.meta", f"   {s.command[:80]}"))
                out.append(("", "\n"))
            sel_shell = shells[sel]
            out.append(("", "\n"))
            out.append(("class:trace.step", f"  ── output · {sel_shell.id} ──\n"))
            tail = sel_shell.tail(40)
            if not tail:
                out.append(("class:trace.dim", "  (no output yet)\n"))
            else:
                for line in tail:
                    out.append(("class:trace.shell", f"  {line}\n"))
            return out

        def hint_text():
            return [
                ("class:hint", "  ↑/↓ "), ("class:hint.key", "navigate"),
                ("class:hint", "   k "), ("class:hint.key", "kill"),
                ("class:hint", "   esc "), ("class:hint.key", "back"),
            ]

        header = Window(FormattedTextControl(header_text), height=1)
        rule = Window(FormattedTextControl(lambda: [("class:rule", "─" * 240)]), height=1)
        body = Window(
            FormattedTextControl(shells_text, focusable=False),
            wrap_lines=False, always_hide_cursor=True,
        )
        hint = Window(FormattedTextControl(hint_text), height=1)
        return HSplit([
            Window(height=1), header, rule, body, rule, hint,
            VSplit([Window(width=2), self._input_window]),
            Window(height=1),
        ])

    _SEV_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

    def _current_findings(self) -> list[Finding]:
        if self.scan is not None and self.mode == "scanning":
            findings = self.scan.findings
        elif self.mode == "viewing":
            findings = self.last_findings
        elif self.scan is not None and self.scan.findings:
            findings = self.scan.findings
        else:
            findings = self.last_findings
        # Sort by severity (critical first), stable so equal-severity findings
        # keep their discovery order.
        return sorted(
            findings,
            key=lambda f: self._SEV_RANK.get((f.severity or "info").lower(), 99),
        )

    @staticmethod
    def _short_target(target: str) -> str:
        try:
            home = str(Path.home())
            if target.startswith(home):
                return "~" + target[len(home):]
        except Exception:
            pass
        return target

    # ── Update banner ────────────────────────────────────────────

    _ANN_LEVEL_STYLE = {
        "info": "class:tip",
        "warning": "class:sev.medium",
        "critical": "class:sev.critical",
    }

    def _update_banner_text(self) -> list[tuple[str, str]]:
        """Render update + announcement banners for the landing screen."""
        info = self._update_info
        if info is None:
            return []
        out: list[tuple[str, str]] = []

        # Update available notification.
        if info.has_update and info.latest:
            from openhack import __version__ as cur
            out.append(("class:sev.medium", f"  ⬆ Update available: {cur} → {info.latest.version}"))
            out.append(("class:tip", "  ·  pipx upgrade openhack"))
            if info.latest.download_url:
                out.append(("class:tip", f"  ·  {info.latest.download_url}"))
            out.append(("", "\n"))

        # Banner-placement announcements.
        for ann in info.announcements:
            if "banner" not in ann.placement:
                continue
            style = self._ANN_LEVEL_STYLE.get(ann.level, "class:tip")
            out.append((style, f"  {ann.title}"))
            if ann.body:
                # Show first line of body as a subtitle.
                first_line = ann.body.split("\n")[0].strip()
                if first_line:
                    out.append(("class:tip", f"  —  {first_line}"))
            out.append(("", "\n"))

        return out

    # ── Invalidate / refresh ──────────────────────────────────────

    def _invalidate(self) -> None:
        try:
            self.app.invalidate()
        except Exception:
            pass

    # ── Input handling ────────────────────────────────────────────

    def _on_buffer_accept(self, buf: Buffer) -> bool:
        text = buf.text.strip()
        buf.reset()
        if not text:
            return False
        if text == "?":
            text = "/help"
        asyncio.create_task(self._dispatch_input(text))
        return False  # keep buffer alive

    async def _dispatch_input(self, text: str) -> None:
        try:
            await self._handle_input(text)
        finally:
            self._invalidate()

    async def _handle_input(self, text: str) -> None:
        # Cancel any pending confirmations when an unrelated input arrives.
        if self._logout_armed and not text.startswith("/logout"):
            self._logout_armed = False
        if self._verify_arm_subject is not None and not text.startswith("/verify"):
            self._verify_arm_subject = None

        # Bang mode: `!<cmd>` runs a shell command directly (no LLM). Handled
        # first, before the running-instruction queue below, so it works in any
        # mode. A trailing ` &` backgrounds it (see _start_shell).
        if text.startswith("!"):
            self._start_shell(text[1:].strip())
            return

        # Non-slash input.
        if not text.startswith("/"):
            low = text.lstrip("-").strip().lower()
            running = self.scan_task is not None and not self.scan_task.done()

            # A run (scan or agent) is actively in flight → queue as a mid-loop
            # instruction; the agent picks it up on its next iteration.
            if running and self.session:
                if low in _CANCEL_PHRASES:
                    self._cancel_scan()
                    return
                self.session.add_user_instruction(text)
                self.last_status_line = "queued — the agent will pick this up mid-run"
                return

            # An agent conversation is open but idle → continue it (remembering
            # everything so far) rather than starting from scratch.
            if self.is_agent_session and self.agent is not None:
                self.active_tab = "trace"  # return from a /findings view to the chat
                self._continue_agent(text)
                return

            # A finished scan is on screen → chat about its findings.
            if self.mode == "scanning" and self.session:
                await self._chat(text)
                return

            # Landing: hand the task to the interactive hacking agent.
            self._start_agent(text)
            return

        parts = text.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd == "/help":
            self._show_help()
        elif cmd in ("/quit", "/exit"):
            if self.mode == "scanning":
                self._cancel_scan()
            self.app.exit()
        elif cmd == "/cancel":
            self._cancel_scan()
        elif cmd == "/pause":
            self._pause_scan()
        elif cmd == "/resume":
            self._resume_scan()
        elif cmd == "/clear":
            self.mode = "landing"
            self.scan = None
            self.agent = None
            self.is_agent_session = False
            self.active_tab = "trace"
            self.viewing_target = ""
            self.viewing_scan_id = ""
            self.last_status_line = ""
        elif cmd == "/login":
            await self._cmd_login()
        elif cmd == "/logout":
            self._cmd_logout()
        elif cmd == "/setup":
            await self._cmd_setup()
        elif cmd == "/provider":
            self._cmd_provider(arg)
        elif cmd == "/connect":
            await self._cmd_connect(arg)
        elif cmd == "/disconnect":
            self._cmd_disconnect(arg)
        elif cmd == "/model":
            self._cmd_model(arg)
        elif cmd == "/sidebar":
            self._toggle_sidebar_visibility()
        elif cmd == "/scan":
            target_path = _resolve_path_argument(arg, default=os.getcwd())
            if not target_path.exists():
                self.last_status_line = f"error: directory not found: {target_path}"
            elif not target_path.is_dir():
                self.last_status_line = f"error: not a directory: {target_path}"
            else:
                self._start_scan(str(target_path))
        elif cmd in ("/cd", "/cwd"):
            self._cmd_cd(arg)
        elif cmd == "/plan":
            if not arg.strip():
                self.last_status_line = 'usage: /plan <objective> — drafts a read-only attack plan'
            else:
                self._start_agent(arg.strip(), plan=True)
        elif cmd == "/cost":
            self._cmd_cost()
        elif cmd == "/findings":
            self._cmd_findings()
        elif cmd == "/config":
            self._cmd_config(arg)
        elif cmd == "/sessions":
            self._open_sessions_overlay()
        elif cmd == "/bashes":
            self._open_shells_view()
        elif cmd == "/copy":
            self._cmd_copy_fix()
        elif cmd == "/verify":
            self._cmd_verify(arg)
        elif cmd == "/mouse":
            self._cmd_mouse(arg)
        elif cmd == "/discord":
            self._cmd_discord()
        else:
            self.last_status_line = f"unknown command: {cmd} — try /help"

    # ── Commands that just update status ──────────────────────────

    def _show_help(self) -> None:
        lines = ["commands: " + ", ".join(c for c, _ in _SLASH_COMMANDS)]
        self.last_status_line = lines[0]

    def _toggle_sidebar_visibility(self) -> None:
        """Toggle the contextual right panel / Findings list."""
        self.findings_list_hidden = not self.findings_list_hidden
        self.last_status_line = (
            "sidebar hidden" if self.findings_list_hidden else "sidebar shown"
        )
        self._invalidate()

    def _move_finding_selection(
        self,
        delta: int,
        *,
        from_mouse: bool = False,
    ) -> None:
        """Move the selected finding; its cursor keeps the list scrolled."""
        if from_mouse:
            self._last_scroll_at = time.monotonic()
        elif time.monotonic() - self._last_scroll_at < 0.4:
            # macOS can emit an arrow event alongside one trackpad gesture.
            return
        findings = self._current_findings()
        if not findings:
            return
        selected = max(
            0,
            min(len(findings) - 1, self.findings_selected + delta),
        )
        if selected == self.findings_selected:
            return
        self.findings_selected = selected
        self._details_scroll = 0
        self._invalidate()

    def _cmd_cd(self, arg: str) -> None:
        """Change the working directory. Everything that reads os.getcwd() —
        /scan target, @-file completion, the agent root, the landing footer —
        follows the new directory."""
        raw = arg.strip()
        if not raw:
            self.last_status_line = f"cwd: {_abbrev_home(os.getcwd())} — usage: /cd <path>"
            return
        try:
            target = _resolve_path_argument(raw)
        except OSError:
            target = Path(os.path.expanduser(raw.removeprefix("@").strip()))
        if not target.exists():
            self.last_status_line = f"error: no such directory: {target}"
            return
        if not target.is_dir():
            self.last_status_line = f"error: not a directory: {target}"
            return
        try:
            os.chdir(target)
        except OSError as e:
            self.last_status_line = f"error: cd failed: {e}"
            return
        # Invalidate the @-file completion index so it rebuilds for the new cwd.
        self._at_index = None
        self.last_status_line = f"cwd → {_abbrev_home(os.getcwd())}"

    def _cmd_provider(self, name: str) -> None:
        from openhack import providers as _providers

        name = name.lower().strip()
        if not name:
            self._open_provider_picker()
            return
        if not _providers.is_known(name):
            self.last_status_line = (
                f"unknown provider: {name} · try one of: "
                + ", ".join(_providers.list_providers())
            )
            return

        self.provider = name
        if name == "openhack":
            self.model = settings.openhack_model_id or "grok-4.5"
            save_user_config({"provider": name, "model": self.model})
            self.last_status_line = f"switched to openhack ({self.model})"
            return

        resolved = _providers.resolve(name)
        if resolved is None:
            self.last_status_line = f"could not resolve provider: {name}"
            return
        self.model = resolved.model
        save_user_config({"provider": name, "model": self.model})
        if resolved.missing_key_env:
            self.last_status_line = (
                f"switched to {name} ({self.model}) — set {resolved.missing_key_env} to use it"
            )
        else:
            self.last_status_line = f"switched to {name} ({self.model})"

    async def _cmd_connect(self, arg: str) -> None:
        if self.mode == "scanning":
            self.last_status_line = "cannot connect a provider while a scan is in progress"
            return
        parts = arg.strip().split()
        provider_id = parts[0] if parts else None
        auth_method = parts[1] if len(parts) > 1 else None
        connected = await self._run_external(
            run_provider_connect(provider_id, auth_method)
        )
        if not connected:
            self.last_status_line = "provider connection cancelled"
            return
        reload_settings()
        cfg = load_user_config()
        self.provider = resolve_provider(
            cfg.get("provider", settings.llm_provider)
        )
        self.model = cfg.get("model") or settings.openhack_model_id
        self.last_status_line = f"connected: {self.provider} · {self.model}"

    def _cmd_disconnect(self, arg: str) -> None:
        from openhack.provider_auth import get_credential, remove_credential

        provider_id = arg.strip().lower() or self.provider
        if provider_id == "openhack":
            self.last_status_line = "use /logout to disconnect your OpenHack account"
            return
        if not get_credential(provider_id):
            self.last_status_line = (
                f"no saved credentials for {provider_id} "
                "(an environment variable may still be active)"
            )
            return
        remove_credential(provider_id)
        if self.provider == provider_id:
            self.provider = "openhack"
            self.model = settings.openhack_model_id or "grok-4.5"
            save_user_config({"provider": self.provider, "model": self.model})
        self.last_status_line = f"disconnected {provider_id}"

    def _cmd_model(self, arg: str) -> None:
        arg = arg.strip()
        if not arg:
            # Open the scrollable picker instead of dumping a string.
            self._open_model_picker()
            return
        self.model = arg
        save_user_config({"model": arg})
        known = self.provider != "openhack" or arg in OPENHACK_MODELS
        self.last_status_line = (
            f"model set to {arg}" if known
            else f"model set to {arg} — note: not in OpenHack's served list, requests may fail"
        )

    # ── Copy finding for AI agent ─────────────────────────────────

    @staticmethod
    def _clipboard_write(text: str) -> tuple[bool, str]:
        """Write *text* to the system clipboard. Returns (success, tool_used)."""
        import subprocess
        import shutil

        for tool, args in (
            ("pbcopy", ["pbcopy"]),                               # macOS
            ("wl-copy", ["wl-copy"]),                             # Wayland
            ("xclip", ["xclip", "-selection", "clipboard"]),      # X11
            ("xsel", ["xsel", "--clipboard", "--input"]),         # X11 alt
            ("clip", ["clip"]),                                   # Windows
        ):
            if shutil.which(args[0]) is None:
                continue
            try:
                proc = subprocess.run(
                    args, input=text.encode("utf-8"),
                    timeout=2, check=False,
                )
                if proc.returncode == 0:
                    return True, tool
            except Exception:
                continue
        return False, ""

    @staticmethod
    def _format_finding_for_agent(f: Finding) -> str:
        """Format the finding as a self-contained prompt for an AI coding agent."""
        lines: list[str] = [
            "Please fix this security vulnerability in my codebase.",
            "",
            f"# {f.title}",
            "",
        ]
        meta_bits = [f"**Severity:** {f.severity.upper()}"]
        if f.category:
            meta_bits.append(f"**Category:** {f.category}")
        if getattr(f, "cvss_score", None):
            meta_bits.append(f"**CVSS:** {f.cvss_score:.1f}")
        lines.append("  •  ".join(meta_bits))
        if f.file_path:
            loc = f.file_path
            if getattr(f, "line_number", None):
                loc += f":{f.line_number}"
            lines.append(f"**Location:** `{loc}`")
        lines.append("")

        if f.description:
            lines += ["## Description", "", f.description, ""]

        snippet = getattr(f, "code_snippet", None)
        if snippet:
            # Try to infer the fence language from the file extension.
            lang = ""
            if f.file_path:
                ext = f.file_path.rsplit(".", 1)[-1].lower() if "." in f.file_path else ""
                lang = {
                    "ts": "typescript", "tsx": "typescript",
                    "js": "javascript", "jsx": "javascript",
                    "py": "python", "rb": "ruby", "go": "go",
                    "rs": "rust", "java": "java", "kt": "kotlin",
                    "c": "c", "cpp": "cpp", "cs": "csharp",
                    "php": "php", "swift": "swift",
                }.get(ext, ext)
            lines += ["## Vulnerable code", "", f"```{lang}", snippet, "```", ""]

        fix = getattr(f, "fix", None)
        if fix:
            lines += ["## Recommended fix", "", fix, ""]

        if f.file_path:
            lines.append(f"Apply the recommended fix to `{f.file_path}`.")

        return "\n".join(lines)

    def _cmd_copy_fix(self) -> None:
        findings = self._current_findings()
        if not findings:
            self.last_status_line = "no finding selected"
            return
        if self.findings_selected >= len(findings):
            self.last_status_line = "no finding selected"
            return
        f = findings[self.findings_selected]
        text = self._format_finding_for_agent(f)
        ok, tool = self._clipboard_write(text)
        if ok:
            self.last_status_line = (
                f"copied {len(text):,} chars to clipboard via {tool} · "
                f"paste into Codex / Claude Code / Cursor"
            )
        else:
            self.last_status_line = (
                "couldn't find a clipboard tool (pbcopy/xclip/wl-copy/clip)"
            )

    # ── Sessions overlay ──────────────────────────────────────────

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        """Cheap liveness check — `kill -0` doesn't actually signal."""
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False

    def _resume_selected_session(self) -> None:
        """Resume an aborted scan by kicking off a fresh scan against the
        same target. The prior aborted scan's data stays preserved as a
        separate session — this is coarse resume (re-scan the target),
        not mid-scan resume from a step. Findings from the old scan can
        still be viewed via Enter on the aborted row.
        """
        if not self.sessions_index:
            return
        row = self.sessions_index[self.sessions_selected]
        target = row.get("target") or ""
        if not target or not Path(target).exists():
            self.last_status_line = f"target no longer exists: {target}"
            return
        status = (row.get("status") or "").lower()
        if status not in ("aborted", "failed", "cancelled"):
            self.last_status_line = f"can only resume aborted/failed scans (this one is {status})"
            return
        self._close_sessions_overlay()
        self._start_scan(target)
        self.last_status_line = f"resuming: re-scanning {self._short_target(target)}"


    def _open_sessions_overlay(self) -> None:
        """Open the sessions picker as a full-screen overlay."""
        self._refresh_sessions_index()
        if self.mode not in ("sessions", "models", "providers", "shells"):
            self.previous_mode = self.mode  # remember where to go back on Esc
        self.mode = "sessions"
        if not self.sessions_index:
            self.last_status_line = "no saved scans yet — completed scans are saved to ~/.openhack/scans/"
        else:
            self.last_status_line = (
                f"{len(self.sessions_index)} session(s) · ↑/↓ navigate · enter load · r resume (aborted) · esc back"
            )

    def _close_sessions_overlay(self) -> None:
        """Return from the sessions overlay to whatever screen the user was on."""
        target_mode = self.previous_mode or "landing"
        self.mode = target_mode
        self.previous_mode = None
        self.last_status_line = ""

    def _open_shells_view(self) -> None:
        """Open the background-shell watcher (/bashes)."""
        # Don't clobber the origin if we're opening from within another overlay
        # (the single previous_mode slot is shared by sessions/models/shells).
        if self.mode not in ("sessions", "models", "providers", "shells"):
            self.previous_mode = self.mode
        self.mode = "shells"
        self.shells_selected = 0
        n = len(self.shells.list())
        if not n:
            self.last_status_line = "no background shells — start one with  !<command> &"
        else:
            self.last_status_line = f"{n} shell(s) · ↑/↓ navigate · k kill · esc back"

    # ── Model picker overlay ──────────────────────────────────────
    def _open_provider_picker(self) -> None:
        """Open the Models.dev-backed provider switcher."""
        from openhack import providers as provider_registry
        from openhack.provider_auth import get_credential

        entries = [
            {
                "id": "openhack",
                "label": "OpenHack",
                "hint": "Recommended · hosted inference",
                "connected": bool(settings.openhack_api_key),
            }
        ]
        for spec in provider_registry.list_provider_specs():
            resolved = provider_registry.resolve(spec.name)
            entries.append(
                {
                    "id": spec.name,
                    "label": spec.label,
                    "hint": spec.hint,
                    "connected": bool(
                        (resolved and not resolved.missing_key_env)
                        or get_credential(spec.name)
                    ),
                }
            )
        self.provider_index = entries
        self.provider_selected = next(
            (
                index
                for index, entry in enumerate(entries)
                if entry["id"] == self.provider
            ),
            0,
        )
        if self.mode not in ("sessions", "models", "providers", "shells"):
            self.previous_mode = self.mode
        self.mode = "providers"
        self.last_status_line = ""
        self._invalidate()

    def _close_provider_picker(self) -> None:
        self.mode = self.previous_mode or "landing"
        self.previous_mode = None
        self._invalidate()

    def _select_provider_from_picker(self) -> None:
        if not self.provider_index:
            return
        selected = self.provider_index[self.provider_selected]
        self._cmd_provider(selected["id"])
        status = self.last_status_line
        self._close_provider_picker()
        self.last_status_line = status

    def _open_model_picker(self) -> None:
        """Open a full-screen, scrollable model picker."""
        from openhack import providers as provider_registry

        self.model_index = provider_registry.provider_models(self.provider)
        if self.model and not any(
            model["id"] == self.model for model in self.model_index
        ):
            self.model_index.insert(
                0, {"id": self.model, "label": self.model, "desc": "Current model"}
            )
        self.model_selected = next(
            (i for i, m in enumerate(self.model_index) if m["id"] == self.model), 0
        )
        if self.mode not in ("sessions", "models", "providers", "shells"):
            self.previous_mode = self.mode
        self.mode = "models"
        self.last_status_line = ""
        self._invalidate()

    def _close_model_picker(self) -> None:
        self.mode = self.previous_mode or "landing"
        self.previous_mode = None
        self._invalidate()

    def _select_model_from_picker(self) -> None:
        if self.model_index:
            chosen = self.model_index[self.model_selected]["id"]
            self.model = chosen
            save_user_config({"model": chosen})
            self.last_status_line = f"model set to {chosen}"
        self._close_model_picker()

    def _refresh_sessions_index(self) -> None:
        scans_dir = Path.home() / ".openhack" / "scans"
        self.sessions_index = []
        if not scans_dir.exists():
            return
        rows: list[tuple[float, dict]] = []
        for p in scans_dir.glob("*.json"):
            try:
                with open(p) as fp:
                    data = json.load(fp)
            except (OSError, json.JSONDecodeError):
                continue
            findings = data.get("findings", []) or []
            sev_counts = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
            for f in findings:
                sev = (f.get("severity") or "info").lower()
                sev_counts[sev] = sev_counts.get(sev, 0) + 1
            top_sev = next((s.upper() for s in ("critical", "high", "medium", "low", "info")
                            if sev_counts.get(s, 0) > 0), "—")
            target = data.get("target_dir") or "(unknown)"
            started = data.get("started_at") or ""
            try:
                started_display = datetime.fromisoformat(started).strftime("%Y-%m-%d %H:%M")
            except ValueError:
                started_display = started[:16]
            duration = data.get("duration_seconds") or 0
            dur_m, dur_s = divmod(int(duration), 60)
            duration_display = f"{dur_m}:{dur_s:02d}"
            scan_id = data.get("scan_id") or p.stem
            short_id = scan_id[:8]

            # Resolve true status: a "running" report whose PID is no longer
            # alive means the terminal closed mid-scan — reclassify as aborted.
            raw_status = (data.get("status") or "completed").lower()
            status = raw_status
            if raw_status == "running":
                pid = data.get("pid")
                if not (isinstance(pid, int) and self._pid_alive(pid)):
                    status = "aborted"

            label = f"{short_id}  {self._short_target(target)}"
            meta = (
                f"[{status}]  {started_display} · {len(findings)} findings · "
                f"top {top_sev} · {duration_display}"
            )
            row = {
                "path": p,
                "scan_id": scan_id,
                "label": label,
                "meta": meta,
                "target": target,
                "status": status,
                "data": data,
            }
            rows.append((p.stat().st_mtime, row))
        rows.sort(key=lambda x: x[0], reverse=True)
        self.sessions_index = [r for _, r in rows]
        if self.sessions_selected >= len(self.sessions_index):
            self.sessions_selected = max(0, len(self.sessions_index) - 1)

    @staticmethod
    def _findings_from_report(data: dict) -> list[Finding]:
        """Parse the findings array from a saved report (camelCase or snake_case)."""
        loaded: list[Finding] = []
        for fd in (data.get("findings") or []):
            try:
                loaded.append(Finding(
                    category=fd.get("category", "") or "",
                    severity=(fd.get("severity") or "info"),
                    title=fd.get("title", "") or "",
                    description=fd.get("description", "") or "",
                    file_path=fd.get("file_path") or fd.get("filePath") or "",
                    line_number=fd.get("line_number") or fd.get("lineNumber"),
                    code_snippet=fd.get("code_snippet") or fd.get("relevantCode"),
                    poc=fd.get("poc"),
                    fix=fd.get("fix") or fd.get("recommendation"),
                    cvss_score=fd.get("cvss_score") or fd.get("cvssScore"),
                    confidence=fd.get("confidence", "medium"),
                    validated=bool(fd.get("validated", False)),
                ))
            except Exception:
                continue
        return loaded

    def _load_selected_session(self) -> None:
        if not self.sessions_index:
            return
        row = self.sessions_index[self.sessions_selected]
        data = row.get("data") or {}
        loaded = self._findings_from_report(data)
        self.last_findings = loaded
        # Build a placeholder scan with a frozen clock. start_time / end_time
        # must share the same time base — we anchor on the first trace
        # event's epoch timestamp so per-event [m:ss] offsets read sanely,
        # then set end_time = start_time + duration so the header shows the
        # actual duration (not start-epoch arithmetic).
        scan = ScanState(target=row.get("target") or "")
        scan.cost = float((data.get("cost") or {}).get("total_cost") or 0.0)
        duration = float(data.get("duration_seconds") or 0)

        # Hydrate saved trace events (version 2+ reports). Older reports
        # have no trace field — Trace tab will show "no trace yet" for those.
        trace_raw = data.get("trace") or []
        first_ts: Optional[float] = None
        for entry_data in trace_raw:
            try:
                entry = TraceEntry(
                    timestamp=float(entry_data.get("timestamp") or 0),
                    agent=entry_data.get("agent", "") or "",
                    event_type=entry_data.get("event_type", "") or "",
                    content=entry_data.get("content"),
                    tool_name=entry_data.get("tool_name"),
                    tool_input=entry_data.get("tool_input"),
                    tool_output=entry_data.get("tool_output"),
                )
                if first_ts is None and entry.timestamp > 0:
                    first_ts = entry.timestamp
                    scan.start_time = first_ts
                scan.update_from_trace(entry)
            except Exception:
                continue

        # Anchor end_time relative to start_time so elapsed_str() reports
        # the actual duration. If no trace events were saved (older reports),
        # fall back to start_time=0 + end_time=duration.
        if first_ts is not None:
            scan.end_time = first_ts + duration
        else:
            scan.start_time = 0
            scan.end_time = duration

        self.scan = scan
        self.viewing_target = row.get("target") or ""
        self.viewing_scan_id = row.get("scan_id") or ""
        self.mode = "viewing"
        self.previous_mode = None
        self.active_tab = "findings"
        self.last_status_line = (
            f"loaded {row.get('scan_id', '')[:8]} · "
            f"{len(loaded)} findings · {row.get('meta', '')}"
        )

    # ── Resume a saved session (openhack --resume <id>) ───────────

    def _resume_session(self, sid: str) -> None:
        """Load a saved session on startup: hydrate the transcript, and for an
        agent session rebuild a continuable agent so a follow-up picks up where
        it left off. Scan sessions open in the findings view."""
        report = Path.home() / ".openhack" / "scans" / f"{sid}.json"
        if not report.exists():
            matches = sorted(report.parent.glob(f"{sid}*.json"))
            if not matches:
                self.last_status_line = f"session {sid[:8]} not found"
                return
            report = matches[0]
        data = json.loads(report.read_text())
        sid = report.stem

        target = data.get("target_dir") or os.getcwd()
        if os.path.isdir(target):
            try:
                os.chdir(target)  # so continued tools run in the session's dir
            except OSError:
                pass

        # Hydrate the transcript (ScanState) from the saved trace.
        findings = self._findings_from_report(data)
        scan = ScanState(target=target)
        scan.cost = float((data.get("cost") or {}).get("total_cost") or 0.0)
        duration = float(data.get("duration_seconds") or 0)
        first_ts: Optional[float] = None
        entries: list[TraceEntry] = []
        for ed in (data.get("trace") or []):
            try:
                entry = TraceEntry(
                    timestamp=float(ed.get("timestamp") or 0),
                    agent=ed.get("agent", "") or "",
                    event_type=ed.get("event_type", "") or "",
                    content=ed.get("content"),
                    tool_name=ed.get("tool_name"),
                    tool_input=ed.get("tool_input"),
                    tool_output=ed.get("tool_output"),
                    event_id=ed.get("event_id"),
                    sequence=ed.get("sequence"),
                    turn_id=ed.get("turn_id"),
                    model_call_id=ed.get("model_call_id"),
                    tool_call_id=ed.get("tool_call_id"),
                    metadata=ed.get("metadata") or {},
                )
                if first_ts is None and entry.timestamp > 0:
                    first_ts = entry.timestamp
                    scan.start_time = first_ts
                entries.append(entry)
                scan.update_from_trace(entry)
            except Exception:
                continue
        scan.findings = list(findings)
        scan.end_time = (first_ts + duration) if first_ts is not None else duration
        if first_ts is None:
            scan.start_time = 0
        self.scan = scan
        self.last_findings = findings

        if data.get("kind") == "agent":
            self._resume_agent(data, target, scan, sid, entries)
        else:
            self.viewing_target = target
            self.mode = "viewing"
            self.active_tab = "findings"
            self.last_status_line = f"resumed {sid[:8]} · {len(findings)} findings (viewing)"

    def _resume_agent(self, data: dict, target: str, scan: "ScanState",
                      sid: str, entries: list) -> None:
        """Rebuild a continuable InteractiveAgent from a saved agent session."""
        reload_settings()
        from openhack.agents.interactive import InteractiveAgent

        # Keep the session's identity. A fresh id would (a) show a stranger's
        # hash in the status line and the exit hint, and (b) make the next
        # _write_report land in a NEW file — forking the session instead of
        # continuing it, leaving the original frozen.
        session = Session(target_dir=target, scan_id=sid, on_trace=self._on_trace)
        session.findings = list(scan.findings)
        # Carry the saved history forward: _write_report serializes
        # session.trace wholesale, so without this the first continued turn
        # would overwrite the file with only that turn's events, destroying the
        # transcript we just restored.
        session.trace = list(entries)
        cost = data.get("cost") or {}
        session.total_cost = float(cost.get("total_cost") or 0.0)
        session.total_tokens = int(cost.get("total_tokens") or 0)
        session.total_input_tokens = int(cost.get("total_input_tokens") or 0)
        session.total_output_tokens = int(cost.get("total_output_tokens") or 0)
        session.status_history = list(data.get("status_history") or session.status_history)
        self.session = session
        # Bubble any new report_finding calls into the ScanState (as _run_agent does).
        _orig_add = session.add_finding
        def _bubble(f, _orig=_orig_add):
            _orig(f)
            if self.scan is not None and f not in self.scan.findings:
                self.scan.findings.append(f)
        session.add_finding = _bubble  # type: ignore[method-assign]

        tools = ToolRegistry(target_dir=Path(target), include_agent_tools=True,
                             session=session, shells=self.shells)
        llm = LLMClient(
            model=data.get("model") or self.model,
            temperature=0.0,
            max_tokens=8192,
            provider=data.get("provider") or self.provider,
            prompt_cache_key=session.id,
        )
        agent = InteractiveAgent(llm, tools, session)
        agent.stream_callback = self._on_agent_stream
        llm.status_callback = self._on_llm_status
        # Seed the agent's message history from the saved trace so a follow-up
        # continues with full context (not a cold start). Setting _system_prompt
        # is what makes continue_run resume rather than fall back to run().
        journal_messages, journal_prompt = self._messages_from_event_journal(
            data, sid
        )
        agent._system_prompt = (
            data.get("system_prompt")
            or journal_prompt
            or agent.get_system_prompt({"target_dir": target})
        )
        saved_messages = data.get("message_history") or []
        if saved_messages:
            agent.messages = [
                Message(
                    role=m.get("role", "user"),
                    content=m.get("content"),
                    tool_calls=m.get("tool_calls"),
                    tool_call_id=m.get("tool_call_id"),
                    name=m.get("name"),
                )
                for m in saved_messages
                if isinstance(m, dict) and m.get("role")
            ]
        elif journal_messages:
            agent.messages = journal_messages
        else:
            agent.messages = self._messages_from_trace(data.get("trace") or [])
        self.agent = agent
        self.is_agent_session = True
        self.mode = "scanning"
        self.active_tab = "trace"
        self.last_status_line = f"resumed {session.id[:8]} · type a follow-up to continue"

    @staticmethod
    def _messages_from_trace(trace: list) -> list[Message]:
        """Reconstruct a valid, continuable message history from saved trace
        events. User turns and the agent's own text become user/assistant
        messages; tool activity is folded into the assistant text as a compact
        note (no synthetic tool_call/result pairs, so the sequence stays valid)."""
        msgs: list[Message] = []
        pending: list[str] = []

        def flush() -> None:
            if pending:
                text = "\n".join(pending).strip()
                if text:
                    msgs.append(Message(role="assistant", content=text))
                pending.clear()

        for ed in trace:
            et = ed.get("event_type")
            content = ed.get("content")
            if et == "user" and isinstance(content, str) and content.strip():
                flush()
                msgs.append(Message(role="user", content=content.strip()))
            elif et == "thinking" and isinstance(content, str) and content.strip():
                pending.append(content.strip())
            elif et == "tool_call":
                name = ed.get("tool_name") or "tool"
                if name == "finish_task":
                    continue
                inp = ed.get("tool_input") or {}
                hint = ""
                if isinstance(inp, dict):
                    for k in ("command", "url", "path", "payload", "pattern", "target", "name"):
                        v = inp.get(k)
                        if isinstance(v, str) and v:
                            hint = v if len(v) <= 120 else v[:117] + "…"
                            break
                pending.append(f"[ran {name} {hint}]".rstrip())
            elif et == "tool_result":
                if ed.get("tool_name") == "finish_task":
                    continue
                out = ed.get("tool_output")
                summ = ""
                if isinstance(out, dict):
                    if "error" in out:
                        summ = f"error: {str(out['error'])[:120]}"
                    elif "exit_code" in out:
                        summ = f"exit {out['exit_code']}"
                    else:
                        summ = str(out)[:160]
                elif out:
                    summ = str(out)[:160]
                if summ:
                    pending.append(f"  → {summ}")
        flush()
        # Start on a user turn so the history is well-formed.
        while msgs and msgs[0].role != "user":
            msgs.pop(0)
        return msgs

    @staticmethod
    def _messages_from_event_journal(
        report: dict, sid: str
    ) -> tuple[list[Message], Optional[str]]:
        """Recover exact messages/config even if the process died before report write."""
        configured = report.get("event_log_path")
        path = (
            Path(configured)
            if configured
            else Path.home() / ".openhack" / "scans" / f"{sid}.events.jsonl"
        )
        if not path.exists():
            return [], None
        messages: list[Message] = []
        system_prompt: Optional[str] = None
        try:
            with open(path, encoding="utf-8", errors="replace") as fp:
                for line in fp:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("event_type") == "agent_configuration":
                        candidate = (event.get("data") or {}).get("system_prompt")
                        if isinstance(candidate, str) and candidate:
                            system_prompt = candidate
                    if event.get("event_type") != "message_appended":
                        continue
                    message = (event.get("data") or {}).get("message") or {}
                    if not isinstance(message, dict) or not message.get("role"):
                        continue
                    messages.append(Message(
                        role=message["role"],
                        content=message.get("content"),
                        tool_calls=message.get("tool_calls"),
                        tool_call_id=message.get("tool_call_id"),
                        name=message.get("name"),
                    ))
        except OSError:
            return [], system_prompt
        return messages, system_prompt

    def _cmd_cost(self) -> None:
        sess = self.last_session or self.session
        if not sess:
            self.last_status_line = "no scan has been run yet"
            return
        b = sess.get_cost_breakdown()
        self.last_status_line = (
            f"cost: ${b['total_cost']:.4f} · tokens: {b['total_tokens']:,}"
        )

    def _cmd_findings(self) -> None:
        findings = self._current_findings()
        if not findings:
            self.last_status_line = (
                "no findings yet — run /scan, or ask the agent to investigate and record findings"
            )
            return
        # Switch to the findings view. Since the tab bar is hidden in agent
        # sessions, esc or typing a message returns to the transcript.
        self.active_tab = "findings"
        self.findings_selected = 0
        self.last_status_line = f"{len(findings)} finding(s) · esc or type to return to chat"

    def _cmd_config(self, arg: str) -> None:
        if not arg.strip():
            cfg = load_user_config()
            self.last_status_line = "config: " + ", ".join(
                f"{k}={'***' if 'api_key' in k and v else v}" for k, v in cfg.items() if v
            )
            return
        parts = arg.strip().split(None, 1)
        key = parts[0].lower()
        value = parts[1] if len(parts) > 1 else ""
        valid = {"provider", "model", "openhack_api_key", "openhack_model_id", "openhack_base_url", "prompt_caching"}
        if key not in valid:
            self.last_status_line = f"unknown config key: {key}"
            return
        if not value:
            cfg = load_user_config()
            current = cfg.get(key, "")
            self.last_status_line = f"{key} = {'***' if 'api_key' in key and current else current or '(not set)'}"
            return
        save_user_config({key: value})
        if key == "provider":
            self.provider = resolve_provider(value)
            from openhack import providers as provider_registry
            spec = provider_registry.get_spec(value)
            self.model = (
                PROVIDER_DEFAULTS.get(value)
                or (spec.default_model if spec else self.model)
            )
            save_user_config({"provider": self.provider, "model": self.model})
        elif key == "model":
            self.model = value
        reload_settings()
        self.last_status_line = f"saved {key}"

    # ── Setup / login (delegate to setup.py / auth.py) ────────────

    async def _cmd_setup(self) -> None:
        if self.mode == "scanning":
            self.last_status_line = "cannot run setup while a scan is in progress"
            return
        # Setup wizard is a separate full-screen flow; we need to suspend our
        # app to let it use the terminal.
        await self._run_external(run_setup_command())
        reload_settings()
        cfg = load_user_config()
        self.provider = resolve_provider(cfg.get("provider", settings.llm_provider))
        self.model = cfg.get("model") or PROVIDER_DEFAULTS.get(self.provider, settings.openhack_model_id)
        self.org_name = cfg.get("openhack_org_name") or self.org_name
        self.last_status_line = f"active: {self.provider} · {self.model}"

    async def _cmd_login(self) -> None:
        if self.mode == "scanning":
            self.last_status_line = "cannot log in while a scan is in progress"
            return
        from openhack.auth import (
            DeviceLoginCancelled,
            DeviceLoginError,
            DeviceLoginExpired,
            device_login,
        )
        cfg = load_user_config()
        app_url = cfg.get("openhack_app_url") or settings.openhack_app_url

        async def _do_login():
            try:
                return await device_login(app_url)
            except DeviceLoginCancelled:
                self.last_status_line = "login cancelled"
            except DeviceLoginExpired as exc:
                self.last_status_line = f"login expired: {exc}"
            except DeviceLoginError as exc:
                self.last_status_line = f"login failed: {exc}"
            return None

        result = await self._run_external(_do_login())
        if not result:
            return
        new_cfg: dict = {"provider": "openhack", "openhack_api_key": result.token}
        if result.org_id: new_cfg["openhack_org_id"] = result.org_id
        if result.org_slug: new_cfg["openhack_org_slug"] = result.org_slug
        if result.org_name: new_cfg["openhack_org_name"] = result.org_name
        if result.user_email: new_cfg["openhack_user_email"] = result.user_email
        if result.user_first_name: new_cfg["openhack_user_first_name"] = result.user_first_name
        if result.user_last_name: new_cfg["openhack_user_last_name"] = result.user_last_name
        save_user_config(new_cfg)
        reload_settings()
        self.org_name = result.org_name or self.org_name
        self.last_status_line = f"logged in · {result.org_name or ''}"

    def _cmd_logout(self) -> None:
        cfg = load_user_config()
        if not cfg.get("openhack_api_key"):
            self.last_status_line = "not signed in"
            return
        first = cfg.get("openhack_user_first_name") or ""
        last = cfg.get("openhack_user_last_name") or ""
        email = cfg.get("openhack_user_email") or ""
        who = " ".join(p for p in (first, last) if p).strip() or email or "current user"
        org = cfg.get("openhack_org_name") or ""
        target = f"{who} · {org}" if org else who
        self._open_modal(
            "logout",
            "Sign out?",
            f"You're about to sign out from {target}.\n\n"
            f"The saved API token will be cleared from ~/.openhack/config. "
            f"You can sign back in any time with /login.",
            self._do_logout,
        )

    def _do_logout(self) -> None:
        cleared = {
            "openhack_api_key": None,
            "openhack_org_id": None,
            "openhack_org_slug": None,
            "openhack_org_name": None,
            "openhack_user_email": None,
            "openhack_user_first_name": None,
            "openhack_user_last_name": None,
        }
        # `save_user_config` merges into the existing JSON, so None values
        # would just be ignored. We need to physically remove them.
        try:
            existing = load_user_config()
            for k in cleared:
                existing.pop(k, None)
            from openhack.config import CONFIG_PATH
            import json as _json, os as _os
            with open(CONFIG_PATH, "w") as fp:
                _json.dump(existing, fp, indent=2)
                fp.write("\n")
            try:
                _os.chmod(CONFIG_PATH, 0o600)
            except OSError:
                pass
        except Exception as exc:
            self.last_status_line = f"sign-out failed: {exc}"
            self._logout_armed = False
            return

        reload_settings()
        self.org_name = ""
        self.user_email = ""
        self.scan = None
        self.session = None
        self.mode = "landing"
        self.active_tab = "trace"
        self._logout_armed = False
        self.last_status_line = "signed out · run /login to sign back in"
        self._invalidate()

    # ── Modal helpers ─────────────────────────────────────────────

    def _open_modal(self, kind: str, title: str, body: str,
                    on_yes: Callable[[], None]) -> None:
        self._modal_kind = kind
        self._modal_title = title
        self._modal_body = body
        self._modal_on_yes = on_yes
        self._invalidate()

    def _close_modal(self) -> None:
        self._modal_kind = None
        self._modal_title = ""
        self._modal_body = ""
        self._modal_on_yes = None

    def _show_announcement_modal(self, ann: Announcement) -> None:
        """Display an announcement as a modal dialog. On dismiss, persist
        the announcement ID so it won't appear again (unless critical)."""
        def _dismiss():
            save_dismissed(ann.id)

        # Critical announcements can't be dismissed without acknowledging.
        title = ann.title or "Announcement"
        body = ann.body or ""
        self._open_modal(f"announcement:{ann.id}", title, body, _dismiss)
        self._invalidate()

    def _cmd_discord(self) -> None:
        url = "https://openhack.com/discord"
        try:
            import webbrowser
            webbrowser.open(url)
            self.last_status_line = f"opened {url} in your browser"
        except Exception as exc:
            self.last_status_line = f"couldn't open browser: {exc} · visit {url}"
        self._invalidate()

    def _cmd_mouse(self, arg: str) -> None:
        """Toggle mouse capture. When off, native terminal drag-to-select works
        (so users can copy text), at the cost of mouse-wheel scrolling and
        click-to-select inside the TUI. Keyboard nav still works either way.
        """
        a = arg.strip().lower()
        if a in ("on", "true", "1"):
            self._mouse_enabled = True
        elif a in ("off", "false", "0"):
            self._mouse_enabled = False
        else:
            self._mouse_enabled = not self._mouse_enabled
        if self._mouse_enabled:
            self.last_status_line = (
                "mouse ON · wheel scroll & click work · /mouse off to enable drag-to-copy"
            )
        else:
            self.last_status_line = (
                "mouse OFF · drag to select & copy text · /mouse on to re-enable"
            )
        self._invalidate()

    # ── Verify (sandbox / browser) ────────────────────────────────

    _VERIFY_PREREQS = {
        "sandbox": (
            "SANDBOX needs: (1) Docker Desktop or daemon running · "
            "(2) Dockerfile OR docker-compose.yml at the scan target's root · "
            "(3) the app must start and respond to a health check on /  · "
            "(4) a free localhost port the sandbox can bind to."
        ),
        "browser": (
            "BROWSER needs: (1) the browser extra installed → "
            "`uv sync --extra browser`  · "
            "(2) Chromium installed → `uv run playwright install chromium` · "
            "(3) the target app reachable over HTTP — usually means sandbox "
            "verification is also on (so the app is running)."
        ),
    }

    def _cmd_verify(self, arg: str) -> None:
        """Run sandbox or browser verification against the currently-loaded
        session's findings. /verify is an *action*, not a settings toggle —
        the user loads a session via /sessions or finishes a scan, then runs
        /verify sandbox or /verify browser to add verification evidence to
        the existing findings.
        """
        parts = arg.strip().split()
        logging.getLogger("openhack.tui").info("/verify dispatched: arg=%r", arg)

        if not parts:
            self.last_status_line = (
                "usage: /verify <sandbox|browser> "
                "— runs verification against the loaded session's findings"
            )
            return

        kind = parts[0].lower()
        if kind not in ("sandbox", "browser"):
            self.last_status_line = f"unknown subject: {kind} (use sandbox/browser)"
            return

        if self.mode == "scanning" and self.scan_task is not None:
            self.last_status_line = "a scan is already running · wait for it to finish first"
            return

        findings = self._current_findings()
        if not findings:
            self.last_status_line = "no findings loaded — finish a scan or load a session from /sessions first"
            return

        # Resolve the target directory: viewing mode stores it on viewing_target,
        # otherwise pull from the currently-loaded session.
        target_dir = (
            self.viewing_target
            or (self.scan.target if self.scan and self.scan.target else "")
        )
        if not target_dir or not Path(target_dir).exists():
            self.last_status_line = (
                f"target directory not accessible: {target_dir or '(unknown)'}"
            )
            return

        title = f"Run {kind} verification on {len(findings)} finding(s)?"
        body = (
            f"{self._VERIFY_PREREQS[kind]}\n\n"
            f"Target: {target_dir}\n"
            f"This will spin up the verification swarm against the loaded findings, "
            f"stream events into the Trace tab, and write a new report to "
            f"~/.openhack/scans/ when it finishes."
        )

        def _apply():
            task = asyncio.create_task(self._run_verification(kind, target_dir, list(findings)))
            self.scan_task = task

        self._open_modal(f"verify:{kind}", title, body, _apply)

    async def _run_verification(self, kind: str, target_dir: str,
                                findings: list[Finding]) -> None:
        """Spin up the sandbox/browser verifier swarm against an existing
        findings set. Streams trace events live and writes a new report when done.
        """
        reload_settings()

        # Preserve the loaded scan's existing trace and findings — verification
        # is an *extension* of an existing scan, not a fresh run. We mutate the
        # current ScanState (created either by the previous scan or by
        # _open_session) so:
        #   • trace_lines / trace_agents from the original scan stay intact
        #   • the new sandbox/browser swarms append to that same trace
        #   • scan.findings already has every finding ready for the Findings tab
        if self.scan is None:
            self.scan = ScanState(target=target_dir)
        # Reset the clock so the elapsed counter reflects this verification run.
        self.scan.start_time = time.time()
        self.scan.end_time = None
        # Ensure scan.findings holds the findings we're about to verify so the
        # Findings tab reads them in scanning mode. (When loaded from /sessions
        # the trace got hydrated but findings live on self.last_findings — we
        # mirror them onto scan.findings here.) Use the *same* objects so the
        # verifier's mutations show up on the rendered list.
        if not self.scan.findings:
            self.scan.findings = list(findings)
        self.last_findings = list(findings)
        self.mode = "scanning"
        self.active_tab = "trace"
        self._invalidate()

        session: Optional[Session] = None
        try:
            session = Session(
                target_dir=target_dir,
                on_trace=self._on_trace,
            )
            # Seed the verifier with the findings being verified — same Finding
            # *objects* as scan.findings so the swarm's mutations are visible
            # in the rendered list without copying.
            for f in findings:
                session.findings.append(f)
            self.session = session

            tools = ToolRegistry(target_dir=Path(target_dir))
            llm = LLMClient(
                model=self.model, temperature=0.0, max_tokens=8192,
                provider=self.provider, prompt_cache_key=session.id,
            )

            if kind == "sandbox":
                from openhack.agents.sandbox_verifier_swarm import SandboxVerifierSwarmAgent
                from openhack.sandbox.orchestrator import SandboxConfig
                sandbox_cfg = SandboxConfig(
                    health_check_path=settings.sandbox_health_check_path,
                    health_check_timeout=settings.sandbox_health_check_timeout,
                    teardown_on_complete=settings.sandbox_teardown_on_complete,
                )
                swarm = SandboxVerifierSwarmAgent(
                    llm, tools, session, sandbox_config=sandbox_cfg,
                )
            else:
                from openhack.agents.browser_verifier_swarm import BrowserVerifierSwarmAgent
                from openhack.sandbox.orchestrator import SandboxConfig
                sandbox_cfg = SandboxConfig(
                    health_check_path=settings.sandbox_health_check_path,
                    health_check_timeout=settings.sandbox_health_check_timeout,
                    teardown_on_complete=settings.sandbox_teardown_on_complete,
                )
                swarm = BrowserVerifierSwarmAgent(
                    llm, tools, session, sandbox_config=sandbox_cfg,
                )

            # The swarm reads findings from context["confirmed_findings"] as dicts.
            findings_dicts = [f.to_dict() for f in findings]
            result = await swarm.run(
                f"Run {kind} verification on the loaded findings.",
                context={"confirmed_findings": findings_dicts},
            )

            # The swarm returns lists of {finding_index, status, evidence, ...}.
            # Stamp the matching Finding objects so the Findings tab can render
            # a ✓ next to verified ones. Findings are mutated in place, which
            # `self.scan.findings` shares — the UI picks up the changes on the
            # next invalidate.
            exploitable = (result or {}).get("exploitable") or []
            verified_by = "sandbox" if kind == "sandbox" else "browser"
            verified_count = 0
            for item in exploitable:
                idx = item.get("finding_index")
                if idx is None or idx >= len(findings):
                    continue
                f = findings[idx]
                # source is a comma-joined string when multiple verifiers have
                # validated the same finding (e.g., "sandbox,browser").
                existing = {s.strip() for s in (f.source or "").split(",") if s.strip()}
                existing.add(verified_by)
                f.source = ",".join(sorted(existing))
                evidence = item.get("evidence")
                if evidence and not f.poc:
                    f.poc = evidence
                verified_count += 1

            # Persist the now-annotated findings so the user can find them in /sessions.
            fatal = (result or {}).get("fatal_error")
            status = "failed" if fatal else "completed"
            self._write_report(session, target_dir, status=status)
            self.last_findings = list(session.findings)
            self.last_session = session
            if fatal:
                self.last_status_line = (
                    f"{kind} verification aborted · {fatal}"
                )
            else:
                self.last_status_line = (
                    f"{kind} verification complete · "
                    f"{verified_count}/{len(findings)} verified · "
                    f"report saved to ~/.openhack/scans/{session.id[:8]}.json"
                )

        except asyncio.CancelledError:
            self.last_status_line = f"{kind} verification cancelled"
            if session is not None:
                self._write_report(session, target_dir, status="cancelled")
            raise
        except Exception as exc:
            self.last_status_line = f"{kind} verification failed: {exc}"
            if session is not None:
                self._write_report(session, target_dir, status="failed")
        finally:
            if self.scan is not None:
                self.scan.finish()
            self.scan_task = None
            self.active_tab = "findings"
            self.findings_selected = 0
            self._invalidate()

    async def _run_external(self, awaitable):
        """Suspend the full-screen app, run an external async flow, then resume."""
        # Prompt_toolkit's run_in_terminal lets us yield the terminal to a
        # non-app process. The 'in_executor=False' default suits async work.
        from prompt_toolkit.application.run_in_terminal import in_terminal
        result_holder = {}

        async def _runner():
            try:
                result_holder["v"] = await awaitable
            except Exception as exc:  # surface any exception
                result_holder["err"] = exc

        async with in_terminal():
            await _runner()
        if "err" in result_holder:
            self.last_status_line = f"error: {result_holder['err']}"
            return None
        return result_holder.get("v")

    # ── Scan kickoff ──────────────────────────────────────────────

    def _start_scan(self, target_dir: str) -> None:
        if self.mode == "scanning":
            self.last_status_line = "a scan is already in progress"
            return
        # Allocate the durable identity before entering the scan view. Project
        # context construction can take a while on a large repo; creating the
        # Session afterward left the footer blank throughout that first phase.
        session = Session(target_dir=target_dir, on_trace=self._on_trace)
        self.session = session
        self.scan = ScanState(target=target_dir)
        self.mode = "scanning"
        self.agent = None
        self.is_agent_session = False
        self.active_tab = "trace"
        self.viewing_target = ""
        self.viewing_scan_id = ""
        self._cancel_armed = False
        self._interrupting = False
        self.scan_task = asyncio.create_task(self._run_scan(target_dir, session))

    def _start_test_scan(self) -> None:
        if self.mode == "scanning":
            self.last_status_line = "a scan is already in progress"
            return
        self.scan = ScanState(target=os.getcwd() + " (test)")
        self.mode = "scanning"
        self.active_tab = "trace"
        self.viewing_target = ""
        self.viewing_scan_id = ""
        self._cancel_armed = False
        self._interrupting = False
        self.scan_task = asyncio.create_task(self._run_test_scan())

    def _start_agent(self, task: str, plan: bool = False) -> None:
        """Start a fresh interactive hacking agent (or plan mode) on a task."""
        if self.scan_task is not None and not self.scan_task.done():
            # A run is in flight — treat this as a follow-up instruction instead.
            if self.session is not None:
                self.session.add_user_instruction(task)
                self.last_status_line = "queued — the agent will pick this up mid-run"
            return
        target_dir = os.getcwd()
        label = "planning" if plan else "agent"
        self.scan = ScanState(target=f"{target_dir} ({label})")
        self.mode = "scanning"
        self.active_tab = "trace"
        self.viewing_target = ""
        self.viewing_scan_id = ""
        self._cancel_armed = False
        self._interrupting = False
        self.scan_task = asyncio.create_task(self._run_agent(task, target_dir, plan))

    async def _run_agent(self, task: str, target_dir: str, plan: bool) -> None:
        reload_settings()
        session: Optional[Session] = None
        try:
            from openhack.agents.interactive import InteractiveAgent, PlanAgent

            session = Session(target_dir=target_dir, on_trace=self._on_trace)
            self.session = session
            # Bubble agent-reported findings (report_finding tool) into the
            # ScanState so /findings can show them.
            _orig_add = session.add_finding
            def _bubble(f, _orig=_orig_add):
                _orig(f)
                if self.scan is not None and f not in self.scan.findings:
                    self.scan.findings.append(f)
            session.add_finding = _bubble  # type: ignore[method-assign]
            # Echo the user's task into the transcript so both sides of the
            # conversation are visible.
            session.add_trace(agent="you", event_type="user", content=task)

            tools = ToolRegistry(target_dir=Path(target_dir), include_agent_tools=True,
                                 session=session, shells=self.shells)
            llm = LLMClient(
                model=self.model, temperature=0.0, max_tokens=8192,
                provider=self.provider, prompt_cache_key=session.id,
            )
            agent_cls = PlanAgent if plan else InteractiveAgent
            agent = agent_cls(llm, tools, session)
            agent.stream_callback = self._on_agent_stream
            llm.status_callback = self._on_llm_status
            self.agent = agent
            self.is_agent_session = True

            # Persist immediately so even a crashed first turn leaves a record.
            self._write_report(session, target_dir, status="running")
            status = "completed"
            result = await agent.run(task, context={"target_dir": target_dir})
            self._finalize_agent_turn(session, agent, result, plan)
        except asyncio.CancelledError:
            self.last_status_line = (
                "interrupted · type a follow-up to continue"
                if self._interrupting
                else "agent stopped"
            )
            status = "cancelled"
            raise
        except Exception as exc:
            self.last_status_line = f"agent error: {exc}"
            status = "failed"
        finally:
            if self.scan is not None:
                self.scan.finish()
            if session is not None:
                self._write_report(session, target_dir, status=status)
            self.scan_task = None
            self._stream_buf = ""
            self._stream_reasoning = ""
            self._stream_tool_bytes = 0
            self._interrupting = False
            self._llm_status = ""
            self._invalidate()

    # ── Shell (bang) mode ─────────────────────────────────────────

    def _start_shell(self, command: str) -> None:
        """Bang mode: run a shell command directly. `<cmd> &` backgrounds it."""
        command = command.strip()
        if not command:
            self.last_status_line = "usage: !<command>   (append & to run in background)"
            self._invalidate()
            return
        if command.endswith("&"):
            self._start_background_shell(command[:-1].strip())
            return
        if self.scan_task is not None and not self.scan_task.done():
            self.last_status_line = "a run is in progress — press Esc to interrupt it first"
            self._invalidate()
            return
        # Reuse an open agent transcript so the command + output land in the same
        # conversation (and feed back to the agent); otherwise start a fresh
        # transcript view for the shell output.
        if self.is_agent_session and self.session is not None and self.scan is not None:
            self.scan.end_time = None  # re-arm the elapsed clock
        else:
            self.scan = ScanState(target=os.getcwd() + " (shell)")
            self.session = Session(target_dir=os.getcwd(), on_trace=self._on_trace)
            self.agent = None
            self.is_agent_session = True
        self.mode = "scanning"
        self.active_tab = "trace"
        self.viewing_target = ""
        self.viewing_scan_id = ""
        self._cancel_armed = False
        self._interrupting = False
        self._shell_active = True
        self.scan_task = asyncio.create_task(self._run_shell(command))

    def _start_background_shell(self, command: str) -> None:
        """Launch `!cmd &` in the background (non-blocking); watch via /bashes."""
        command = command.strip()
        if not command:
            self.last_status_line = "usage: !<command> &"
            self._invalidate()
            return
        try:
            sid = self.shells.spawn(command, cwd=os.getcwd())
        except Exception as exc:
            self.last_status_line = f"could not start background shell: {exc}"
            self._invalidate()
            return
        self.last_status_line = f"started {sid} in background · /bashes to watch · !{command} &"
        # Note it in the transcript when one is on screen.
        if self.session is not None and self.scan is not None:
            self.session.add_trace(
                agent="shell", event_type="shell_bg",
                content={"id": sid, "command": command},
            )
        self._invalidate()

    async def _run_shell(self, command: str) -> None:
        """Stream a shell command's output into the transcript. Esc-interruptible."""
        import codecs

        from openhack.tools.process import kill_process_group

        session = self.session
        proc = None
        output_tail: list[str] = []

        def _emit(text: str) -> None:
            output_tail.append(text)
            if len(output_tail) > 200:
                output_tail.pop(0)
            session.add_trace(agent="shell", event_type="shell_output", content=text)

        try:
            session.add_trace(agent="you", event_type="user", content="! " + command)
            session.add_trace(agent="shell", event_type="shell_start", content=command)
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=os.getcwd(),
                start_new_session=True,  # own process group → killable as a tree
            )
            self._shell_proc = proc
            # Chunked reads (not readline): asyncio's readline caps a single line
            # at 64KB and raises on overflow — minified JS/JSON/base64 blow past
            # that. Read raw chunks and split into lines ourselves, flushing a
            # monster line rather than buffering unboundedly.
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            buf = ""
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                buf += decoder.decode(chunk)
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    _emit(line)
                if len(buf) > 65536:
                    _emit(buf)
                    buf = ""
            buf += decoder.decode(b"", final=True)
            if buf:
                _emit(buf)
            rc = await proc.wait()
            session.add_trace(agent="shell", event_type="shell_end", content={"exit_code": rc})
            self.last_status_line = f"! {command} · exit {rc}"
            # Feed the result back to an open agent so it has the output as context.
            if self.agent is not None:
                tail = "\n".join(output_tail[-80:])
                session.add_user_instruction(
                    f"[ran shell command] $ {command}\n(exit {rc})\n{tail}"
                )
        except asyncio.CancelledError:
            session.add_trace(agent="shell", event_type="shell_end", content={"interrupted": True})
            self.last_status_line = "shell interrupted"
            raise
        except Exception as exc:
            session.add_trace(agent="shell", event_type="shell_end", content={"error": str(exc)})
            self.last_status_line = f"shell error: {exc}"
        finally:
            # Kill the child on ANY early exit (cancel, read error, etc.) so it
            # can't outlive the transcript as an untracked orphan.
            if proc is not None and proc.returncode is None:
                kill_process_group(proc)
            self._shell_proc = None
            self._shell_active = False
            if self.scan is not None:
                self.scan.finish()
            self.scan_task = None
            self._interrupting = False
            self._invalidate()

    def _continue_agent(self, task: str) -> None:
        """Continue the open agent conversation with a follow-up turn."""
        if self.agent is None:
            self._start_agent(task)
            return
        # Re-arm the spinner/elapsed clock for the new turn while keeping the
        # existing transcript on screen.
        if self.scan is not None:
            self.scan.end_time = None
        self.scan_task = asyncio.create_task(self._run_continue(task))

    async def _run_continue(self, task: str) -> None:
        session = self.session
        agent = self.agent
        status = "completed"
        try:
            session.add_trace(agent="you", event_type="user", content=task)
            result = await agent.continue_run(task)
            self._finalize_agent_turn(session, agent, result, plan=False)
        except asyncio.CancelledError:
            self.last_status_line = (
                "interrupted · type a follow-up to continue"
                if self._interrupting
                else "agent stopped"
            )
            status = "cancelled"
            raise
        except Exception as exc:
            self.last_status_line = f"agent error: {exc}"
            status = "failed"
        finally:
            if self.scan is not None:
                self.scan.finish()
            if session is not None:
                self._write_report(session, session.target_dir, status=status)
            self.scan_task = None
            self._stream_buf = ""
            self._stream_reasoning = ""
            self._stream_tool_bytes = 0
            self._interrupting = False
            self._llm_status = ""
            self._invalidate()

    def _finalize_agent_turn(self, session, agent, result: dict, plan: bool) -> None:
        # Surface the final answer in the transcript if the model ended on a
        # tool call with no closing prose. When it ended with text, BaseAgent
        # already traced it — don't duplicate.
        final = (result.get("response") or result.get("partial_result") or "").strip()
        already = any(
            e.event_type == "thinking" and (e.content or "").strip() == final
            for e in session.trace[-3:]
        )
        if final and not already:
            session.add_trace(agent=agent.name, event_type="thinking", content=final)
        self.last_session = session
        self.last_status_line = (
            f"{'plan' if plan else 'agent'} done · {session.total_tokens:,} tokens · "
            f"${session.total_cost:.4f} · type a follow-up or /clear"
        )

    def _cancel_scan(self) -> None:
        if self.mode != "scanning":
            self.last_status_line = "no scan is running"
            return
        self.last_status_line = "cancelling…"
        if self.session:
            self.session.cancel()
        if self.scan_task and not self.scan_task.done():
            self.scan_task.cancel()

    def _interrupt_run(self) -> None:
        """ESC during a run — stop the agent/scan but keep the transcript and
        any findings so the user can ask a follow-up (unlike /cancel, which
        tears the session down)."""
        if self.scan_task is None or self.scan_task.done():
            return
        self._interrupting = True
        self.last_status_line = "interrupting…"
        if self.session is not None:
            self.session.cancel()
        self.scan_task.cancel()
        self._invalidate()

    def _pause_scan(self) -> None:
        if self.mode != "scanning" or self.session is None:
            self.last_status_line = "no scan is running"
            return
        if self.session.paused:
            self.last_status_line = "scan is already paused · /resume to continue"
            return
        self.session.pause()
        self.last_status_line = "scan paused · /resume to continue · /cancel to stop"
        self._invalidate()

    def _resume_scan(self) -> None:
        if self.mode != "scanning" or self.session is None:
            self.last_status_line = "no scan is running"
            return
        if not self.session.paused:
            self.last_status_line = "scan is not paused"
            return
        self.session.resume()
        self.last_status_line = "scan resumed"
        self._invalidate()

    def _on_llm_status(self, text: str) -> None:
        """Transient LLM-client state (upstream retry/backoff) for the spinner
        line. An empty string means the call recovered — clear it so a retry
        notice can't outlive the problem it described."""
        self._llm_status = text or ""
        self._invalidate()

    def _processing_verb(self) -> str:
        """A short word for what the agent is doing right now."""
        if self._interrupting:
            return "interrupting"
        if self._shell_active:
            return "running"
        if self._stream_tool_bytes:
            return "writing"
        if self._stream_buf:
            return "responding"
        if self._stream_reasoning:
            return "thinking"
        if not self.is_agent_session:
            return "scanning"
        return "working"

    def _on_agent_stream(self, kind: str, delta: str) -> None:
        """Accumulate streamed tokens (answer + reasoning) and repaint the tail."""
        if not delta:
            return
        if kind == "content":
            self._stream_buf += delta
        elif kind == "reasoning":
            self._stream_reasoning += delta
        elif kind == "tool_args":
            self._stream_tool_bytes += len(delta)
        else:
            return
        # Throttle repaints so a fast stream doesn't thrash the renderer.
        now = time.monotonic()
        if now - self._stream_last_invalidate >= 0.03:
            self._stream_last_invalidate = now
            self._invalidate()

    def _stream_line(self) -> list[tuple[str, str]]:
        """Render the in-progress turn at the transcript tail: the answer as it
        streams, a live reasoning tail, or — before anything streams — an
        animated spinner so waiting never looks frozen."""
        frame = _SPINNER_FRAMES[self._spin_idx % len(_SPINNER_FRAMES)]
        if self._stream_buf:
            text = self._stream_buf
            if len(text) > 4000:
                text = "…" + text[-4000:]
            # Lead with the spinner, not a static bar: this branch is exactly
            # when the verb flips to "responding", and a still ▌ here made the
            # transcript look stalled while only the bottom bar kept moving.
            # The solid ▌ arrives when the message is committed, so a moving
            # spinner reads as "in progress", a bar as "done".
            return [
                ("class:spinner", f" {frame} "),
                ("class:trace.stream", text),
                ("class:trace.agent.bar", "▌"),  # caret marking the live cursor
            ]
        # Checked before reasoning: once arguments start flowing the thinking
        # tail is finished and frozen, so showing it would read as stalled while
        # the real work — a file being written out token by token — is invisible.
        if self._stream_tool_bytes:
            kb = self._stream_tool_bytes / 1024
            size = f"{kb:.1f} KB" if kb >= 1 else f"{self._stream_tool_bytes} B"
            return [
                ("class:spinner", f" {frame} "),
                ("class:trace.dim", f"writing tool input · {size}"),
            ]
        if self._stream_reasoning:
            reasoning = self._stream_reasoning.strip().replace("\n", " ")
            if len(reasoning) > 160:
                reasoning = "…" + reasoning[-160:]
            return [
                ("class:spinner", f" {frame} "),
                ("class:trace.dim", "thinking · "),
                ("class:trace.dim", reasoning),
            ]
        # Nothing streamed yet — a live spinner + verb so the wait feels alive.
        return [
            ("class:spinner", f" {frame} "),
            ("class:trace.dim", self._processing_verb() + "…"),
        ]

    def _on_trace(self, entry: TraceEntry) -> None:
        if self.scan is None:
            return
        # The turn's text just committed as a trace line — drop the live buffers
        # so we don't render the same text twice.
        if entry.event_type in ("thinking", "tool_call"):
            self._stream_buf = ""
            self._stream_reasoning = ""
            self._stream_tool_bytes = 0
        self.scan.update_from_trace(entry)
        # Live-tick the elapsed clock by invalidating.
        self._invalidate()

    async def _run_scan(
        self,
        target_dir: str,
        session: Optional[Session] = None,
    ) -> None:
        reload_settings()
        try:
            # Keep the optional construction path for direct/internal callers,
            # while /scan always supplies the preallocated visible session.
            if session is None:
                session = Session(target_dir=target_dir, on_trace=self._on_trace)
                self.session = session
            project_context = build_project_context(target_dir)
            session.project_context = project_context

            # Wrap on_trace to also persist on key milestones (step_complete,
            # finding_added) so a crashed scan still leaves a readable report.
            def _checkpoint(entry: TraceEntry) -> None:
                self._on_trace(entry)
                if entry.event_type in ("step_complete", "swarm_complete", "finding_added"):
                    self._write_report(session, target_dir, status="running")

            session._on_trace = _checkpoint  # type: ignore[attr-defined]

            # Wrap add_finding to bubble findings into ScanState + persist.
            original_add_finding = session.add_finding

            def _patched_add_finding(f: Finding) -> None:
                original_add_finding(f)
                if self.scan is not None:
                    self.scan.findings.append(f)
                self._write_report(session, target_dir, status="running")
                self._invalidate()

            session.add_finding = _patched_add_finding  # type: ignore[method-assign]

            # Write an initial 'running' report so /sessions sees it immediately.
            self._write_report(session, target_dir, status="running")

            tools = ToolRegistry(target_dir=Path(target_dir))
            llm = LLMClient(
                model=self.model, temperature=0.0, max_tokens=8192,
                provider=self.provider, prompt_cache_key=session.id,
            )
            coordinator = CoordinatorAgent(llm, tools, session)
            await coordinator.run_full_scan()

            self.last_session = session
            self.last_findings = list(session.findings)
            self._write_report(session, target_dir, status="completed")
            self.last_status_line = (
                f"scan complete · {len(session.findings)} findings · "
                f"${session.total_cost:.4f}"
            )

        except asyncio.CancelledError:
            if session is not None:
                self._write_report(session, target_dir, status="cancelled")
                if self._interrupting:
                    n = len(session.findings)
                    self.last_status_line = (
                        f"scan interrupted · {n} finding(s) kept · "
                        "ask about them, or /scan to restart"
                    )
                else:
                    self.last_status_line = (
                        f"scan cancelled · resume with: openhack --resume {session.id}"
                    )
            else:
                self.last_status_line = (
                    "scan interrupted" if self._interrupting else "scan cancelled"
                )
            raise
        except Exception as exc:
            if session is not None:
                self._write_report(session, target_dir, status="failed")
                self.last_status_line = (
                    f"scan failed: {exc} · retry with: openhack --resume {session.id}"
                )
            else:
                self.last_status_line = f"scan failed: {exc}"
        finally:
            if self.scan is not None:
                self.scan.finish()
            self.scan_task = None
            self._interrupting = False
            # On scan completion, jump from Trace → Findings so the user
            # lands on the results without having to switch tabs.
            self.active_tab = "findings"
            self.findings_selected = 0
            self._invalidate()

    def _write_report(
        self,
        session: Session,
        target_dir: str,
        status: Optional[str] = None,
    ) -> None:
        """Atomically write the scan report. Called incrementally during a scan
        (status='running') and at end (status='completed'/'cancelled'/'failed').
        """
        try:
            if status in {"running", "completed", "cancelled", "failed", "paused"}:
                session.transition_status(status, "report_status")
            session.record_event(
                "report_write_started",
                {"status": status, "target_dir": target_dir},
                agent="system",
            )
            report_dir = Path.home() / ".openhack" / "scans"
            report_dir.mkdir(parents=True, exist_ok=True)
            report_path = report_dir / f"{session.id}.json"
            elapsed = time.time() - (self.scan.start_time if self.scan else session.created_at)

            # Serialize trace entries so the Trace tab can re-render later.
            def _trace_dict(e: TraceEntry) -> dict:
                return {
                    "timestamp": e.timestamp,
                    "agent": e.agent,
                    "event_type": e.event_type,
                    "content": redact(e.content),
                    "tool_name": e.tool_name,
                    "tool_input": redact(e.tool_input),
                    "tool_output": redact(e.tool_output),
                    "event_id": e.event_id,
                    "sequence": e.sequence,
                    "turn_id": e.turn_id,
                    "model_call_id": e.model_call_id,
                    "tool_call_id": e.tool_call_id,
                    "metadata": redact(e.metadata),
                }

            # First user message doubles as a human-readable title for /sessions.
            title = next(
                (str(e.content) for e in session.trace if e.event_type == "user"), ""
            )[:140]
            report = {
                "version": 3,
                "event_schema_version": 1,
                "kind": "agent" if self.is_agent_session else "scan",
                "title": title,
                "scan_id": session.id,
                "target_dir": target_dir,
                "provider": self.provider,
                "model": self.model,
                "status": status or session.status.value,
                "status_history": session.status_history,
                "parent_session_id": session.parent_session_id,
                "trace_id": session.trace_id,
                "event_log_path": session.event_log_path,
                "event_count": len(session.events),
                "event_log_error": session.journal.last_error,
                "pid": os.getpid(),
                "started_at": datetime.fromtimestamp(session.created_at).isoformat(),
                "duration_seconds": round(elapsed, 2),
                "cost": session.get_cost_breakdown(),
                "findings": [f.to_dict() for f in session.findings],
                "trace": [_trace_dict(e) for e in session.trace],
                "message_history": [
                    {
                        k: v
                        for k, v in redact(m.to_dict()).items()
                        if k != "reasoning_content"
                    }
                    for m in (
                        self.agent.messages
                        if self.agent is not None
                        and getattr(self.agent, "session", None) is session
                        else []
                    )
                ],
                "system_prompt": (
                    getattr(self.agent, "_system_prompt", None)
                    if self.agent is not None
                    and getattr(self.agent, "session", None) is session
                    else None
                ),
            }
            # Atomic write: temp file + rename to avoid corrupting on crash.
            tmp_path = report_path.with_suffix(".json.tmp")
            with open(tmp_path, "w") as fp:
                json.dump(report, fp, indent=2, default=str, ensure_ascii=False)
            os.replace(tmp_path, report_path)
            session.record_event(
                "report_write_completed",
                {
                    "path": str(report_path),
                    "status": report["status"],
                    "bytes": report_path.stat().st_size,
                },
                agent="system",
            )
        except Exception as exc:
            session.record_event(
                "report_write_failed",
                {
                    "status": status,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
                agent="system",
            )
            logger.warning("Failed to write session report", exc_info=True)

    async def _run_test_scan(self) -> None:
        import random
        from openhack.agents.session import Session as _S

        session = _S(target_dir=os.getcwd(), on_trace=self._on_trace)
        self.session = session

        # Hook live add_finding.
        original_add_finding = session.add_finding

        def _patched_add_finding(f: Finding) -> None:
            original_add_finding(f)
            if self.scan is not None:
                self.scan.findings.append(f)
            self._invalidate()

        session.add_finding = _patched_add_finding  # type: ignore[method-assign]

        def _d():
            return random.uniform(0.05, 0.25)

        try:
            session.add_trace("coordinator", "step_start", "Step 1: Reconnaissance")
            await asyncio.sleep(_d())
            for tool in ["get_project_info", "list_dir", "read_file", "get_route_map",
                         "check_dependencies", "grep", "find_dangerous_patterns"]:
                session.add_trace("recon", "tool_call", "",
                                  tool_name=tool, tool_input={"path": "src"})
                await asyncio.sleep(_d())
                session.add_trace("recon", "tool_result", "", tool_name=tool,
                                  tool_output={"ok": True})
            session.add_trace("coordinator", "step_complete",
                              {"step": "recon", "cost": 0.04, "tokens": 85000})

            groups = ["input_validation", "access_control", "data_handling"]
            session.add_trace("hunter_swarm", "swarm_start",
                              {"groups": groups, "group_count": len(groups)})
            for g in groups:
                a = f"hunter:{g}"
                for tool in ["read_file", "grep", "trace_variable"]:
                    session.add_trace(a, "tool_call", "",
                                      tool_name=tool, tool_input={"path": "src/lib/auth.ts"})
                    await asyncio.sleep(_d())
                    session.add_trace(a, "tool_result", "", tool_name=tool)

            findings = [
                ("IDOR", "critical", "src/app/dashboard/[id]/page.tsx",
                 "IDOR in workspace page — no ownership check"),
                ("SQL Injection", "critical", "src/lib/db.ts",
                 "SQL Injection via queryRawUnsafe"),
                ("XSS", "high", "src/components/note-card.tsx",
                 "Stored XSS via dangerouslySetInnerHTML"),
                ("Auth Bypass", "high", "src/app/api/users/route.ts",
                 "Missing auth check on user list endpoint"),
                ("Open Redirect", "medium", "src/app/api/auth/callback/route.ts",
                 "Unvalidated redirect URL in OAuth callback"),
            ]
            for cat, sev, fp, title in findings:
                session.add_finding(Finding(
                    category=cat, severity=sev, title=title,
                    description=title, file_path=fp,
                ))
                await asyncio.sleep(_d())

            session.add_trace("hunter_swarm", "swarm_complete",
                              {"total_findings": len(findings), "total_cost": 0.18})
            session.add_trace("coordinator", "step_complete",
                              {"step": "hunters", "cost": 0.18, "tokens": 320000})

            session.total_cost = 0.22
            session.status = SessionStatus.COMPLETED
            self.last_session = session
            self.last_findings = list(session.findings)
            self.last_status_line = (
                f"test scan complete · {len(session.findings)} findings"
            )
        except asyncio.CancelledError:
            self.last_status_line = "test scan cancelled"
            raise
        finally:
            if self.scan is not None:
                self.scan.finish()
            self.scan_task = None
            self.active_tab = "findings"
            self.findings_selected = 0
            self._invalidate()

    # ── Chat ──────────────────────────────────────────────────────

    async def _chat(self, user_message: str) -> None:
        self.chat_history.append(Message(role="user", content=user_message))
        reload_settings()
        try:
            llm = LLMClient(
                model=self.model, temperature=0.3, max_tokens=4096,
                provider=self.provider,
            )
        except Exception as exc:
            self.last_status_line = f"llm error: {exc}"
            self.chat_history.pop()
            return

        context_parts = [CHAT_SYSTEM_PROMPT]
        if self.last_session and self.last_session.findings:
            summary = []
            for i, f in enumerate(self.last_session.findings, 1):
                summary.append(
                    f"{i}. [{f.severity.upper()}] {f.category} - {f.title}"
                    + (f" ({f.file_path})" if f.file_path else "")
                )
            context_parts.append("\n\nCurrent scan findings:\n" + "\n".join(summary))

        self.last_status_line = "thinking…"
        self._invalidate()
        try:
            response: LLMResponse = await llm.chat(
                messages=self.chat_history, system="".join(context_parts),
            )
        except Exception as exc:
            self.last_status_line = f"llm error: {exc}"
            self.chat_history.pop()
            return

        reply = (response.content or "").strip() or "(no response)"
        self.chat_history.append(Message(role="assistant", content=reply))
        if len(self.chat_history) > 40:
            self.chat_history = self.chat_history[-30:]
        # Show the reply as a short status line; full reply truncated for
        # the status bar — better display will come in v2.
        self.last_status_line = reply if len(reply) <= 200 else reply[:197] + "…"

    # ── Run ───────────────────────────────────────────────────────

    async def run(self) -> None:
        # Animate the Codex-style shimmer at ~31fps and tick the elapsed clock
        # while a scan is running. The spinner advances every other frame so it
        # remains readable at the higher shimmer refresh rate.
        async def _ticker():
            frame = 0
            while True:
                await asyncio.sleep(0.032)
                if self.mode == "scanning" and self.scan is not None and self.scan.end_time is None:
                    frame += 1
                    if frame % 2 == 0:
                        self._spin_idx = (self._spin_idx + 1) % len(_SPINNER_FRAMES)
                    self._invalidate()
                elif self.mode == "scanning":
                    frame += 1
                    if frame % 31 == 0:
                        self._invalidate()
                elif self.mode == "shells":
                    # Keep the /bashes tail live while a background shell runs,
                    # and fire one repaint on the running→stopped edge so the
                    # final output + exit badge render without a keypress.
                    running = any(s.is_running() for s in self.shells.list())
                    if running or self._shells_were_running:
                        frame += 1
                        if frame % 3 == 0 or (self._shells_were_running and not running):
                            self._invalidate()
                    self._shells_were_running = running

        async def _check_updates():
            info = await fetch_updates()
            if info is None:
                return
            self._update_info = info
            self._invalidate()
            # If there are modal-placement announcements, queue the first one
            # as a modal dialog after a short delay (so it doesn't fight the
            # landing screen initial render).
            modal_anns = [a for a in info.announcements if "modal" in a.placement]
            if modal_anns:
                await asyncio.sleep(0.5)
                self._show_announcement_modal(modal_anns[0])

        tick_task = asyncio.create_task(_ticker())
        asyncio.create_task(_check_updates())
        try:
            await self.app.run_async()
        finally:
            tick_task.cancel()
            # Kill any background shells so quitting doesn't orphan them.
            # shutdown() escalates to SIGKILL synchronously (kill_all()'s daemon
            # Timer would be abandoned when the interpreter exits right after).
            self.shells.shutdown()
            if self.scan_task and not self.scan_task.done():
                # Kill any subprocess a tool is blocked on before tearing down.
                # asyncio.run() waits on the executor's worker thread after this
                # returns, so without killing the child, quitting mid-tool hangs
                # the (already-erased) terminal until the command's own timeout.
                if self.session is not None:
                    self.session.cancel()
                self.scan_task.cancel()
                try:
                    await self.scan_task
                except (asyncio.CancelledError, Exception):
                    pass


def _configure_logging() -> None:
    """Route all logging to a file so messages don't corrupt the full-screen UI.

    Anything that calls `logger.warning(...)` / `logger.error(..., exc_info=True)`
    (e.g. LLMClient retries, upstream errors) would otherwise hit stderr and
    overlap the layout. The log file lives at ~/.openhack/logs/openhack.log.
    """
    log_dir = Path.home() / ".openhack" / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    log_path = log_dir / "openhack.log"

    root = logging.getLogger()
    # Remove any existing StreamHandlers that would write to the terminal.
    for h in list(root.handlers):
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
            root.removeHandler(h)
    # Add our file handler if not already there.
    have_file = any(
        isinstance(h, logging.FileHandler) and Path(getattr(h, "baseFilename", "")) == log_path
        for h in root.handlers
    )
    if not have_file:
        fh = logging.FileHandler(log_path)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(fh)
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)


def _resume_hint(app, scans_dir: Optional[Path] = None) -> Optional[str]:
    """Claude-style 'resume this session' line for the current session, or None
    if there's nothing resumable (no session, or its report was never saved)."""
    if app is None:
        return None
    sess = getattr(app, "session", None) or getattr(app, "last_session", None)
    sid = getattr(sess, "id", None)
    if not sid:
        return None
    scans_dir = scans_dir or (Path.home() / ".openhack" / "scans")
    if not (scans_dir / f"{sid}.json").exists():
        return None
    return f"  Resume this session:  openhack --resume {sid}"


def main(resume_session_id: Optional[str] = None):
    app = None

    def _restore_terminal() -> None:
        # Pop the Kitty keyboard protocol so we never leave the terminal in a
        # foreign keyboard state — including on SIGHUP/SIGTERM, which os._exit
        # past the finally below.
        try:
            if app is not None and getattr(app, "kitty_active", False):
                from openhack import kitty_keys

                kitty_keys.disable()
        except Exception:
            pass

    def _on_fatal_signal(*_):
        _restore_terminal()
        os._exit(1)

    signal.signal(signal.SIGHUP, _on_fatal_signal)
    signal.signal(signal.SIGTERM, _on_fatal_signal)
    _configure_logging()

    app = OpenHackApp(resume_session_id=resume_session_id)
    if getattr(app, "kitty_active", False):
        from openhack import kitty_keys

        kitty_keys.enable()
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        pass
    finally:
        _restore_terminal()

    # Printed after the TUI has torn down (erase_when_done + terminal restored),
    # so it lands in normal scrollback like Claude Code's resume hint.
    hint = _resume_hint(app)
    if hint:
        print("\n" + hint + "\n")


# ── Back-compat aliases for existing imports ──────────────────────

OpenHackCLI = OpenHackApp  # legacy name used by __main__.py
