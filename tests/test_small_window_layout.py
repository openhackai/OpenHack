"""Small terminals should degrade quietly instead of replacing the UI."""

from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.containers import Window

from openhack.tui import HSplit, VSplit


def _fallback_text(split) -> str:
    control = split.window_too_small.content
    assert isinstance(control, FormattedTextControl)
    fragments = control.text() if callable(control.text) else control.text
    return "".join(text for _, text in fragments)


def test_splits_do_not_use_prompt_toolkit_window_too_small_message():
    child = Window(height=20, width=80)

    assert "too small" not in _fallback_text(HSplit([child])).lower()
    assert "too small" not in _fallback_text(VSplit([child])).lower()
