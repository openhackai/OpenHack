"""Retries must not corrupt the TUI, and must not leave a stale notice behind.

Session cd7d02b8 glitched because llm.py print()ed retry messages straight to
stdout while the full-screen app owned the screen: prompt_toolkit doesn't know
about that text, so it tears the layout and survives until the region happens
to be redrawn — which is why the message "stayed in the UI after it recovered".
"""

import asyncio
from pathlib import Path

import openai
import pytest

from openhack.agents.llm import LLMClient, LLMResponse, Message
from openhack.config import settings


def _no_backoff(monkeypatch):
    async def _sleep(*a, **k):
        return None
    monkeypatch.setattr(asyncio, "sleep", _sleep)


def test_library_code_never_prints():
    """A print() from library code lands inside the TUI's screen buffer."""
    import subprocess
    root = Path(__file__).resolve().parent.parent / "openhack"
    hits = subprocess.run(
        ["grep", "-rn", r"^\s*print(", str(root / "agents"), str(root / "tools")],
        capture_output=True, text=True,
    ).stdout.strip()
    # interactive_runner is the deliberate non-TUI console path.
    offenders = [l for l in hits.splitlines() if "interactive_runner" not in l]
    assert not offenders, "print() in TUI-reachable code corrupts the display:\n" + "\n".join(offenders)


def test_retry_reports_status_instead_of_printing(monkeypatch, capsys):
    llm = LLMClient(provider="openhack")
    monkeypatch.setattr(settings, "openhack_max_retries", 2)
    _no_backoff(monkeypatch)
    seen: list[str] = []
    llm.status_callback = seen.append

    calls = {"n": 0}

    async def _flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise openai.APITimeoutError(request=None)
        raise openai.APITimeoutError(request=None)

    monkeypatch.setattr(llm.client.chat.completions, "create", _flaky)
    with pytest.raises(openai.APITimeoutError):
        asyncio.run(llm._chat([Message(role="user", content="hi")]))

    # Nothing written to the terminal…
    assert capsys.readouterr().out == ""
    # …and the UI was told what's happening, with the wait surfaced.
    assert seen and "retrying in" in seen[0]
    assert "APITimeoutError" in seen[0]


def test_status_is_cleared_once_the_call_recovers(monkeypatch, capsys):
    """The notice must not outlive the problem it described."""
    llm = LLMClient(provider="openhack")
    monkeypatch.setattr(settings, "openhack_max_retries", 3)
    _no_backoff(monkeypatch)
    seen: list[str] = []
    llm.status_callback = seen.append

    calls = {"n": 0}

    class _Stream:
        def __aiter__(self):
            async def _gen():
                if False:
                    yield None
            return _gen()

    async def _recovering(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise openai.APITimeoutError(request=None)
        return _Stream()

    monkeypatch.setattr(llm.client.chat.completions, "create", _recovering)
    asyncio.run(llm._chat([Message(role="user", content="hi")]))

    assert capsys.readouterr().out == ""
    assert any("retrying in" in s for s in seen)
    assert seen[-1] == "", "a recovered call must clear the retry notice"


def test_status_callback_is_optional(monkeypatch):
    """Headless callers leave it unset; a bad hook can't break a run."""
    llm = LLMClient(provider="openhack")
    llm._status("anything")            # unset → no-op
    llm.status_callback = lambda _: 1 / 0
    llm._status("boom")                # raising hook is swallowed
