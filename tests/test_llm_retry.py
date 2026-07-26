"""LLMClient retries transient upstream errors, including mid-stream APIError."""

import asyncio

import openai
import pytest

from openhack.agents.llm import LLMClient, Message
from openhack.config import settings


def test_base_apierror_is_retried(monkeypatch):
    # A transient upstream error delivered as a *base* openai.APIError — e.g. an
    # SSE 'server_error' injected mid-stream by a gateway/provider (AtlasCloud's
    # "服务发生异常，请重试") rather than as a 5xx HTTP status — must be retried
    # with backoff, not raised on the first failure and surfaced as "agent error".
    llm = LLMClient(provider="openhack")
    monkeypatch.setattr(settings, "openhack_max_retries", 2)

    async def _nosleep(*a, **k):
        return None

    monkeypatch.setattr(asyncio, "sleep", _nosleep)  # skip the 5s/10s backoff

    calls = {"n": 0}

    async def _boom(**kwargs):
        calls["n"] += 1
        raise openai.APIError("服务发生异常 (server_error)", request=None, body=None)

    monkeypatch.setattr(llm.client.chat.completions, "create", _boom)

    with pytest.raises(openai.APIError):
        asyncio.run(llm._chat([Message(role="user", content="hi")]))

    assert calls["n"] == 3  # initial attempt + 2 retries (not 1 = no-retry)
