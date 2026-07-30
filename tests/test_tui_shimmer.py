"""Codex-style working-label shimmer."""

from openhack.tui import (
    OH_MUTED,
    _SHIMMER_PADDING,
    _SHIMMER_PERIOD_SECONDS,
    _rgb,
    _shimmer_fragments,
)


def _style_color(style: str) -> tuple[int, int, int]:
    color = style.split("fg:#", 1)[1]
    return tuple(int(color[i:i + 2], 16) for i in (0, 2, 4))


def test_shimmer_preserves_label_as_per_character_fragments():
    fragments = _shimmer_fragments("Working", elapsed=0.0)

    assert "".join(text for _, text in fragments) == "Working"
    assert len(fragments) == len("Working")
    assert all(style.startswith("bold fg:#") for style, _ in fragments)


def test_shimmer_highlight_moves_and_repeats():
    first = _shimmer_fragments("Working", elapsed=0.4)
    moved = _shimmer_fragments("Working", elapsed=0.8)
    repeated = _shimmer_fragments(
        "Working",
        elapsed=0.4 + _SHIMMER_PERIOD_SECONDS,
    )

    assert first != moved
    assert first == repeated


def test_shimmer_has_a_smooth_bright_center():
    label = "Working"
    center_index = 3
    period = len(label) + _SHIMMER_PADDING * 2
    elapsed = (
        (center_index + _SHIMMER_PADDING)
        / period
        * _SHIMMER_PERIOD_SECONDS
    )
    colors = [
        _style_color(style)
        for style, _ in _shimmer_fragments(label, elapsed=elapsed)
    ]

    assert sum(colors[center_index]) > sum(_rgb(OH_MUTED))
    assert sum(colors[center_index]) >= sum(colors[center_index - 1])
    assert sum(colors[center_index - 1]) >= sum(colors[center_index - 2])
