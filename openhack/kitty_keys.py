"""Kitty keyboard protocol (CSI-u) support for the TUI.

Legacy terminal input collapses distinct keypresses onto the same bytes:
Option/Alt+Backspace and a plain Backspace both arrive as ``\\x7f``, and a lone
Escape is indistinguishable from the start of an escape sequence. The Kitty
keyboard protocol fixes this — when an app sends ``CSI > 1 u`` the terminal (if
it supports the protocol, e.g. iTerm2 3.5+, kitty, Ghostty, WezTerm) reports
keys as unambiguous ``CSI <code> ; <mods> u`` events instead.

prompt_toolkit (3.0.52) doesn't speak the protocol, so this module bolts it on:

* ``KittyVt100Parser`` recognizes a complete CSI-u sequence, translates it to
  the *legacy* byte string it corresponds to (e.g. ``\\x1b[127;3u`` ->
  ``\\x1b\\x7f`` for Alt+Backspace), and replays those bytes through a stock
  parser. Output is therefore byte-for-byte identical to what the legacy path
  would have produced — every existing key binding keeps working unchanged —
  except the previously-ambiguous cases now arrive correctly.
* ``KittyVt100Input`` swaps that parser into prompt_toolkit's posix input.
* ``enable``/``disable`` push and pop the protocol on the terminal. Terminals
  that don't support it ignore the request, so this is safe to always send.

Only the "disambiguate escape codes" flag (1) is requested, so functional keys
(arrows, F-keys, Home/End) keep their legacy encodings and never reach the
CSI-u path.
"""

from __future__ import annotations

import re
import sys
from typing import Callable, Optional

from prompt_toolkit.input.vt100 import Vt100Input
from prompt_toolkit.input.vt100_parser import Vt100Parser
from prompt_toolkit.key_binding.key_processor import KeyPress

__all__ = [
    "KittyVt100Parser",
    "KittyVt100Input",
    "csi_u_to_legacy",
    "enable",
    "disable",
    "ENABLE_SEQUENCE",
    "DISABLE_SEQUENCE",
]

# Push "disambiguate escape codes" (flag 1); pop on teardown. The push/pop pair
# uses the terminal's own protocol stack, so teardown can't leave a foreign
# state behind as long as disable() runs.
ENABLE_SEQUENCE = "\x1b[>1u"
DISABLE_SEQUENCE = "\x1b[<u"

# A complete CSI-u key event: ESC [ <params> u  (params are digits, ';' and ':').
_CSI_U_RE = re.compile(r"^\x1b\[[0-9;:]+u\Z")

# Kitty modifier field is 1 + bitmask.
_SHIFT = 0b1
_ALT = 0b10
_CTRL = 0b100

# Codepoints the protocol assigns to text-ish keys that have a legacy byte.
_SPECIAL_BASE = {
    27: "\x1b",   # Escape
    13: "\r",     # Enter
    9: "\t",      # Tab
    127: "\x7f",  # Backspace
    32: " ",      # Space
}


def csi_u_to_legacy(codepoint: int, bitmask: int) -> Optional[str]:
    """Return the legacy byte string for a CSI-u (codepoint, modifier-bitmask).

    Returns None for codepoints we don't fabricate input for (functional-key
    PUA codepoints etc.), so the caller can drop the event rather than inventing
    a keystroke. bitmask bits: 1=shift, 2=alt, 4=ctrl (super/meta/locks ignored
    — they have no legacy representation).
    """
    shift = bool(bitmask & _SHIFT)
    alt = bool(bitmask & _ALT)
    ctrl = bool(bitmask & _CTRL)

    # Shift+Tab is BackTab in legacy encoding (its own CSI sequence).
    if codepoint == 9 and shift and not ctrl and not alt:
        return "\x1b[Z"

    if codepoint in _SPECIAL_BASE:
        base = _SPECIAL_BASE[codepoint]
    elif 33 <= codepoint <= 126:
        base = chr(codepoint)
    else:
        return None  # functional / non-ASCII codepoint — don't fabricate input

    if ctrl:
        # Fold into the C0 control code, matching what the terminal sends for
        # Ctrl+<key> in legacy mode (Ctrl+C -> \x03, Ctrl+A -> \x01, ...).
        if 97 <= codepoint <= 122:      # a-z
            base = chr(codepoint - 96)
        elif 64 <= codepoint <= 95:     # @ A-Z [ \ ] ^ _
            base = chr(codepoint - 64)
        elif codepoint == 32:           # Ctrl+Space -> NUL
            base = "\x00"
        # else: Ctrl doesn't change this key's byte (Ctrl+Enter/Tab/Backspace,
        # Ctrl+digit) — best effort: leave the base byte as-is.
    elif shift and 97 <= codepoint <= 122:
        # Bare Shift+letter usually stays legacy, but be safe: uppercase it.
        base = chr(codepoint - 32)

    if alt:
        # Alt/Meta is an Escape prefix in legacy encoding.
        base = "\x1b" + base

    return base


def _csi_u_sequence_to_legacy(sequence: str) -> Optional[str]:
    """Translate a full ``\\x1b[...u`` sequence to its legacy byte string."""
    body = sequence[2:-1]  # strip leading CSI ("\x1b[") and trailing "u"
    fields = body.split(";")
    # First sub-field of each parameter (ignore ':' alternates / event types).
    try:
        codepoint = int(fields[0].split(":")[0])
    except (ValueError, IndexError):
        return None
    mod_val = 1
    if len(fields) > 1 and fields[1]:
        try:
            mod_val = int(fields[1].split(":")[0])
        except ValueError:
            mod_val = 1
    bitmask = mod_val - 1 if mod_val > 0 else 0
    return csi_u_to_legacy(codepoint, bitmask)


class _KittyKey:
    """Marker returned by ``_get_match`` for a complete CSI-u event, carrying the
    legacy byte string it should be replayed as (None => drop the event)."""

    __slots__ = ("legacy",)

    def __init__(self, legacy: Optional[str]) -> None:
        self.legacy = legacy


class KittyVt100Parser(Vt100Parser):
    """Vt100 parser that also understands CSI-u key events."""

    def __init__(self, feed_key_callback: Callable[[KeyPress], None]) -> None:
        super().__init__(feed_key_callback)
        # A stock parser turns the legacy-equivalent bytes of a CSI-u event into
        # KeyPress objects — guaranteeing identical output to the legacy path.
        self._legacy_out: list[KeyPress] = []
        self._legacy_parser = Vt100Parser(self._legacy_out.append)

    def _get_match(self, prefix: str):
        if _CSI_U_RE.match(prefix):
            return _KittyKey(_csi_u_sequence_to_legacy(prefix))
        return super()._get_match(prefix)

    def _call_handler(self, key, insert_text: str) -> None:
        if isinstance(key, _KittyKey):
            legacy = key.legacy
            if not legacy:
                return  # undecodable CSI-u — drop rather than fabricate a key
            self._legacy_out.clear()
            self._legacy_parser.feed_and_flush(legacy)
            for key_press in self._legacy_out:
                self.feed_key_callback(key_press)
            self._legacy_out.clear()
            return
        super()._call_handler(key, insert_text)


class KittyVt100Input(Vt100Input):
    """posix Vt100 input that parses CSI-u key events."""

    def __init__(self, stdin) -> None:
        super().__init__(stdin)
        self.vt100_parser = KittyVt100Parser(
            lambda key_press: self._buffer.append(key_press)
        )


def enable(stream=None) -> None:
    """Ask the terminal to start reporting disambiguated (CSI-u) key events."""
    stream = stream if stream is not None else sys.stdout
    try:
        stream.write(ENABLE_SEQUENCE)
        stream.flush()
    except Exception:
        pass


def disable(stream=None) -> None:
    """Pop the keyboard-protocol change so the terminal is left as we found it."""
    stream = stream if stream is not None else sys.stdout
    try:
        stream.write(DISABLE_SEQUENCE)
        stream.flush()
    except Exception:
        pass
