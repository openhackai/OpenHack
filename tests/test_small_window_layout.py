"""Small terminals should explain why the regular UI cannot be rendered."""

from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.containers import Window

from openhack.tui import HSplit, VSplit


def _fallback_text(split) -> str:
    control = split.window_too_small.content
    assert isinstance(control, FormattedTextControl)
    fragments = control.text() if callable(control.text) else control.text
    return "".join(text for _, text in fragments)


def test_splits_render_an_informative_small_window_fallback():
    child = Window(height=20, width=80)

    for split in (HSplit([child]), VSplit([child])):
        text = _fallback_text(split).lower()
        assert "openhack" in text
        assert "window too small" in text
        assert "enlarge your terminal" in text
