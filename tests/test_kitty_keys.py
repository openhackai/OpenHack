"""Kitty keyboard protocol (CSI-u) decoding.

The contract: parsing a CSI-u event must yield the *exact same* KeyPress
sequence as parsing the legacy bytes it corresponds to — so every existing key
binding keeps working, while the previously-ambiguous cases (Option+Backspace,
lone Escape) now arrive correctly.
"""

import pytest
from prompt_toolkit.input.vt100_parser import Vt100Parser

from openhack.kitty_keys import (
    DISABLE_SEQUENCE,
    ENABLE_SEQUENCE,
    KittyVt100Parser,
    csi_u_to_legacy,
    disable,
    enable,
)


def _parse(parser_cls, data):
    out = []
    p = parser_cls(out.append)
    p.feed_and_flush(data)
    return [(kp.key, kp.data) for kp in out]


def _legacy(data):
    return _parse(Vt100Parser, data)


def _kitty(data):
    return _parse(KittyVt100Parser, data)


# (label, CSI-u bytes, legacy-equivalent bytes)
GOLDEN = [
    ("Option/Alt+Backspace", "\x1b[127;3u", "\x1b\x7f"),
    ("lone Escape",          "\x1b[27u",    "\x1b"),
    ("Ctrl+C",               "\x1b[99;5u",  "\x03"),
    ("Ctrl+A",               "\x1b[97;5u",  "\x01"),
    ("Ctrl+D",               "\x1b[100;5u", "\x04"),
    ("Enter",                "\x1b[13u",    "\r"),
    ("Tab",                  "\x1b[9u",     "\t"),
    ("Shift+Tab",            "\x1b[9;2u",   "\x1b[Z"),
    ("Alt+b",                "\x1b[98;3u",  "\x1bb"),
    ("Alt+f",                "\x1b[102;3u", "\x1bf"),
    ("Space",                "\x1b[32u",    " "),
    ("Ctrl+Alt+c",           "\x1b[99;7u",  "\x1b\x03"),
    ("plain a (no mods)",    "\x1b[97u",    "a"),
]


@pytest.mark.parametrize("label,csi_u,legacy", GOLDEN, ids=[g[0] for g in GOLDEN])
def test_csi_u_matches_legacy(label, csi_u, legacy):
    assert _kitty(csi_u) == _legacy(legacy), label


def test_alt_backspace_is_escape_then_backspace():
    # The whole point: Option+Backspace becomes (Escape, ControlH) — which the
    # TUI's word-delete binding fires on — instead of a bare backspace.
    keys = [k for k, _ in _kitty("\x1b[127;3u")]
    assert keys == [k for k, _ in _legacy("\x1b\x7f")]
    assert len(keys) == 2  # Escape + Backspace, not a single Backspace


def test_ctrl_c_still_ctrl_c_under_protocol():
    # Regression guard: enabling the protocol must NOT break the interrupt key.
    assert _kitty("\x1b[99;5u") == _legacy("\x03")


def test_plain_typing_passes_through_unchanged():
    assert _kitty("hello world") == _legacy("hello world")


def test_legacy_sequences_still_parse_through_kitty_parser():
    # Arrows / function keys stay legacy under flag 1 and must be untouched.
    for seq in ("\x1b[A", "\x1b[B", "\x1b[1;3D", "\x1b[3~", "\x1bOP"):
        assert _kitty(seq) == _legacy(seq), seq


def test_csi_u_split_across_feeds_still_decodes():
    # Bytes can arrive in separate reads; the parser must wait for the 'u'.
    out = []
    p = KittyVt100Parser(out.append)
    p.feed("\x1b[127;3")   # partial — no terminator yet
    assert out == []       # nothing emitted while incomplete
    p.feed("u")            # completes the sequence
    assert [(kp.key, kp.data) for kp in out] == _legacy("\x1b\x7f")


def test_undecodable_csi_u_is_dropped_not_crashed():
    # A functional-key PUA codepoint we don't map -> no key, no exception.
    assert csi_u_to_legacy(57352, 0) is None
    assert _kitty("\x1b[57352;3u") == []


def test_bracketed_paste_still_works():
    pasted = _kitty("\x1b[200~hello\x1b[201~")
    assert any("hello" in (data or "") for _, data in pasted)


def test_enable_disable_write_push_and_pop():
    class _Buf:
        def __init__(self):
            self.text = ""
        def write(self, s):
            self.text += s
        def flush(self):
            pass

    b = _Buf()
    enable(b)
    disable(b)
    assert b.text == ENABLE_SEQUENCE + DISABLE_SEQUENCE
    assert ENABLE_SEQUENCE == "\x1b[>1u"
    assert DISABLE_SEQUENCE == "\x1b[<u"


def test_enable_disable_never_raise_on_bad_stream():
    class _Broken:
        def write(self, s):
            raise IOError("nope")
        def flush(self):
            pass

    enable(_Broken())   # must not raise
    disable(_Broken())
