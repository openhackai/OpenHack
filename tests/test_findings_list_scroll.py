"""Overflowing findings lists follow selection and accept wheel navigation."""

from types import SimpleNamespace

from openhack.tui import OpenHackApp, _findings_list_cursor_row


def _finding(path="", source=""):
    return SimpleNamespace(file_path=path, source=source)


def test_cursor_row_accounts_for_header_badge_and_variable_finding_height():
    findings = [
        _finding("src/a.py"),
        _finding(),
        _finding("src/c.py", "sandbox"),
        _finding("src/d.py"),
    ]

    # Header is three rows because a verification badge is present.
    # Previous entries consume three, two, and three rows respectively.
    assert _findings_list_cursor_row(findings, 0) == 3
    assert _findings_list_cursor_row(findings, 1) == 6
    assert _findings_list_cursor_row(findings, 2) == 8
    assert _findings_list_cursor_row(findings, 3) == 11


def test_mouse_wheel_moves_selection_and_resets_details_scroll():
    app = OpenHackApp.__new__(OpenHackApp)
    findings = [_finding(f"src/{i}.py") for i in range(20)]
    app._current_findings = lambda: findings
    app.findings_selected = 0
    app._details_scroll = 9
    app._last_scroll_at = 0.0
    app._invalidate = lambda: None

    app._move_finding_selection(3, from_mouse=True)

    assert app.findings_selected == 3
    assert app._details_scroll == 0
    assert app._last_scroll_at > 0


def test_finding_selection_clamps_to_available_rows():
    app = OpenHackApp.__new__(OpenHackApp)
    findings = [_finding() for _ in range(5)]
    app._current_findings = lambda: findings
    app.findings_selected = 3
    app._details_scroll = 0
    app._last_scroll_at = 0.0
    app._invalidate = lambda: None

    app._move_finding_selection(99)
    assert app.findings_selected == 4

    app._move_finding_selection(-99)
    assert app.findings_selected == 0
