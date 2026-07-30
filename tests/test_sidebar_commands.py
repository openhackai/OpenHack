"""Sidebar controls should match their documented behavior."""

import asyncio

from openhack.tui import OpenHackApp


def _app():
    app = OpenHackApp.__new__(OpenHackApp)
    app._logout_armed = False
    app._verify_arm_subject = None
    app.findings_list_hidden = False
    app._invalidate = lambda: None
    return app


def test_sidebar_slash_command_toggles_contextual_sidebar():
    app = _app()

    asyncio.run(app._handle_input("/sidebar"))
    assert app.findings_list_hidden is True
    assert app.last_status_line == "sidebar hidden"

    asyncio.run(app._handle_input("/sidebar"))
    assert app.findings_list_hidden is False
    assert app.last_status_line == "sidebar shown"
