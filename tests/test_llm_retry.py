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


class _Stream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)

    async def close(self):
        pass


def _chunk(content="ok", model="gpt-5.6-sol"):
    from types import SimpleNamespace
    return SimpleNamespace(
        id="resp-1",
        model=model,
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                delta=SimpleNamespace(content=content, reasoning_content=None, tool_calls=None),
            )
        ],
    )


def _make_bad_request_error(message: str, param: str = None):
    import httpx
    req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    body = {"error": {"message": message, "type": "invalid_request_error"}}
    if param:
        body["error"]["param"] = param
    res = httpx.Response(400, request=req, json=body)
    return openai.BadRequestError(message, response=res, body=body)


def test_unsupported_temperature_value_is_retried_and_remembered(monkeypatch):
    LLMClient._temperature_unsupported_models.clear()
    llm = LLMClient(model="gpt-5.6-sol", temperature=0.0)
    calls = []

    async def _fake_create(**kwargs):
        calls.append(dict(kwargs))
        if "temperature" in kwargs:
            raise _make_bad_request_error(
                "Unsupported value: 'temperature' does not support 0.0 with this model",
                param="temperature",
            )
        return _Stream([_chunk()])

    monkeypatch.setattr(llm.client.chat.completions, "create", _fake_create)

    resp = asyncio.run(llm.chat([Message(role="user", content="hi")]))
    assert resp.content == "ok"
    assert len(calls) == 2
    assert calls[0].get("temperature") == 0.0
    assert "temperature" not in calls[1]
    assert "gpt-5.6-sol" in LLMClient._temperature_unsupported_models

    # Subsequent call with same model omits temperature from the start
    calls.clear()
    resp2 = asyncio.run(llm.chat([Message(role="user", content="hello again")]))
    assert resp2.content == "ok"
    assert len(calls) == 1
    assert "temperature" not in calls[0]


def test_unsupported_temperature_parameter_is_retried(monkeypatch):
    LLMClient._temperature_unsupported_models.clear()
    llm = LLMClient(model="gpt-6.1-sol", temperature=0.0)
    calls = []

    async def _fake_create(**kwargs):
        calls.append(dict(kwargs))
        if "temperature" in kwargs:
            raise _make_bad_request_error(
                "Unsupported parameter: 'temperature' is not supported with this model"
            )
        return _Stream([_chunk(model="gpt-6.1-sol")])

    monkeypatch.setattr(llm.client.chat.completions, "create", _fake_create)

    resp = asyncio.run(llm.chat([Message(role="user", content="hi")]))
    assert resp.content == "ok"
    assert len(calls) == 2
    assert calls[0].get("temperature") == 0.0
    assert "temperature" not in calls[1]
    assert "gpt-6.1-sol" in LLMClient._temperature_unsupported_models


def test_unrelated_bad_request_error_is_raised(monkeypatch):
    LLMClient._temperature_unsupported_models.clear()
    llm = LLMClient(model="gpt-5.6-sol", temperature=0.0)
    calls = []

    async def _fake_create(**kwargs):
        calls.append(dict(kwargs))
        raise _make_bad_request_error("Context length exceeded", param="messages")

    monkeypatch.setattr(llm.client.chat.completions, "create", _fake_create)

    with pytest.raises(openai.BadRequestError):
        asyncio.run(llm.chat([Message(role="user", content="hi")]))

    assert len(calls) == 1
    assert "gpt-5.6-sol" not in LLMClient._temperature_unsupported_models


def test_prompt_cache_and_temperature_both_rejected(monkeypatch):
    LLMClient._temperature_unsupported_models.clear()
    LLMClient._cache_key_unsupported = False
    llm = LLMClient(model="gpt-5.6-sol", temperature=0.0, prompt_cache_key="test-key")
    calls = []

    async def _fake_create(**kwargs):
        calls.append(dict(kwargs))
        if len(calls) == 1:
            # First attempt rejects prompt_cache_key
            raise _make_bad_request_error("Unsupported parameter: 'prompt_cache_key'")
        if len(calls) == 2:
            # Second attempt rejects temperature
            raise _make_bad_request_error("Unsupported parameter: 'temperature'")
        return _Stream([_chunk()])

    monkeypatch.setattr(llm.client.chat.completions, "create", _fake_create)

    resp = asyncio.run(llm.chat([Message(role="user", content="hi")]))
    assert resp.content == "ok"
    assert len(calls) == 3
    assert "prompt_cache_key" in calls[0]
    assert "prompt_cache_key" not in calls[1]
    assert "temperature" in calls[1]
    assert "prompt_cache_key" not in calls[2]
    assert "temperature" not in calls[2]

