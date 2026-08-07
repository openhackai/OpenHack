"""The shared prompt composer accepts and displays multi-line instructions."""

import asyncio
from types import SimpleNamespace

from prompt_toolkit.buffer import Buffer
from prompt_toolkit.keys import Keys

from openhack.tui import OpenHackApp


def _binding(app: OpenHackApp, keys, handler_name: str):
    return next(
        binding
        for binding in app.kb.bindings
        if tuple(binding.keys) == tuple(keys)
        and binding.handler.__name__ == handler_name
    )


def test_main_composer_is_multiline_and_auto_growing():
    app = OpenHackApp()

    assert app.input_buffer.multiline()
    assert app._input_window.height.min == 1
    assert app._input_window.height.max == 6
    assert app._input_window.wrap_lines()


def test_multiline_paste_is_dispatched_without_losing_lines():
    app = OpenHackApp()
    prompt = (
        "Reproduce WP2Shell against the provided test sandbox. Write a minimal PoC and\n"
        "  verify remote code execution non-destructively."
    )
    dispatched = []

    async def capture(text: str) -> None:
        dispatched.append(text)

    async def submit() -> None:
        app._dispatch_input = capture
        app.input_buffer.text = prompt
        app._on_buffer_accept(app.input_buffer)
        await asyncio.sleep(0)

    asyncio.run(submit())

    assert dispatched == [prompt]
    assert app.input_buffer.text == ""


def test_newline_shortcuts_insert_a_line_break():
    app = OpenHackApp()
    newline = _binding(
        app,
        (Keys.Escape, Keys.ControlM),
        "_composer_newline",
    )
    buffer = Buffer(multiline=True)
    buffer.text = "first line"
    buffer.cursor_position = len(buffer.text)

    newline.handler(SimpleNamespace(current_buffer=buffer))

    assert buffer.text == "first line\n"


def test_enter_submit_does_not_steal_empty_sessions_navigation():
    app = OpenHackApp()
    submit = _binding(app, (Keys.ControlM,), "_submit_composer")

    app.mode = "landing"
    assert submit.filter()

    app.mode = "sessions"
    app.input_buffer.reset()
    assert not submit.filter()

    app.input_buffer.text = "continue investigating"
    assert submit.filter()
