"""Codex-style working-label shimmer."""

from openhack.tui import (
    OH_MUTED,
    _SHIMMER_HALF_WIDTH,
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


def test_shimmer_highlight_has_a_smooth_bright_center():
    label = "Working"
    center_index = 3
    period = len(label) + _SHIMMER_PADDING * 2
    elapsed = (
        (center_index + _SHIMMER_PADDING)
        / period
        * _SHIMMER_PERIOD_SECONDS
    )
    fragments = _shimmer_fragments(label, elapsed=elapsed)
    colors = [_style_color(style) for style, _ in fragments]
    base = _rgb(OH_MUTED)

    assert sum(colors[center_index]) > sum(base)
    assert sum(colors[center_index]) >= sum(colors[center_index - 1])
    assert sum(colors[center_index - 1]) >= sum(colors[center_index - 2])


def test_shimmer_sweeps_over_time_and_repeats_cleanly():
    first = _shimmer_fragments("Working", elapsed=0.4)
    moved = _shimmer_fragments("Working", elapsed=0.8)
    repeated = _shimmer_fragments(
        "Working",
        elapsed=0.4 + _SHIMMER_PERIOD_SECONDS,
    )

    assert first != moved
    assert first == repeated


def test_shimmer_band_width_matches_codex_motion():
    label = "Working"
    period = len(label) + _SHIMMER_PADDING * 2
    center_position = _SHIMMER_PADDING
    elapsed = center_position / period * _SHIMMER_PERIOD_SECONDS
    fragments = _shimmer_fragments(label, elapsed=elapsed)

    # The leading character is at the center; a character within the five-cell
    # half-width is highlighted while one beyond it is at the base color.
    assert _style_color(fragments[4][0]) != _rgb(OH_MUTED)
    assert _SHIMMER_HALF_WIDTH == 5.0
    assert _style_color(fragments[6][0]) == _rgb(OH_MUTED)
