import asyncio
from types import SimpleNamespace

import pytest

from openhack.agents.llm import LLMClient, Message
from openhack.config import settings


class _Stream:
    def __init__(self, chunks, terminal_error=None):
        self.chunks = list(chunks)
        self.terminal_error = terminal_error

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.chunks:
            return self.chunks.pop(0)
        if self.terminal_error:
            raise self.terminal_error
        raise StopAsyncIteration

    async def close(self):
        return None


def _delta(
    *, content=None, tool_calls=None, reasoning_content=None, reasoning_details=None
):
    return SimpleNamespace(
        content=content,
        tool_calls=tool_calls,
        reasoning_content=reasoning_content,
        reasoning_details=reasoning_details,
    )


def _chunk(delta, finish_reason=None, usage=None):
    return SimpleNamespace(
        id="response-1",
        model="returned-model",
        usage=usage,
        choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)],
    )


def _client(stream):
    llm = LLMClient.__new__(LLMClient)
    llm.provider = "test"
    llm.model = "requested-model"
    llm.max_tokens = 64
    llm.temperature = 0
    llm.prompt_cache_key = None
    llm._resolved = None
    llm.status_callback = None
    llm.total_cost = 0
    llm.total_tokens = 0
    llm.total_input_tokens = 0
    llm.total_output_tokens = 0
    llm.PRICING = {}
    events = []
    llm.event_callback = lambda event_type, data, **kw: events.append(
        (event_type, data, kw)
    )

    async def create(**kwargs):
        return stream

    llm.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    return llm, events


def test_model_response_logs_finish_reason_ids_and_timing(monkeypatch):
    monkeypatch.setattr(settings, "openhack_max_retries", 0)
    stream = _Stream([
        _chunk(_delta(content="hello"), finish_reason="stop"),
    ])
    llm, events = _client(stream)
    response = asyncio.run(llm._chat([Message(role="user", content="hi")]))

    assert response.finish_reason == "stop"
    assert response.response_id == "response-1"
    assert response.returned_model == "returned-model"
    assert response.latency_seconds >= 0
    completed = next(data for kind, data, _ in events if kind == "model_call_completed")
    assert completed["finish_reason"] == "stop"
    assert completed["content"] == "hello"


def test_openrouter_reasoning_details_are_concatenated_for_replay(monkeypatch):
    monkeypatch.setattr(settings, "openhack_max_retries", 0)
    first = {
        "type": "reasoning.text",
        "text": "inspect ",
        "id": "reasoning-1",
        "format": "openrouter-v1",
        "index": 0,
    }
    second = SimpleNamespace(
        type="reasoning.text",
        text="the result",
        id="reasoning-2",
        format="openrouter-v1",
        index=1,
    )
    stream = _Stream([
        _chunk(_delta(reasoning_details=[first])),
        _chunk(_delta(reasoning_details=[second]), finish_reason="stop"),
    ])
    llm, _ = _client(stream)

    response = asyncio.run(llm._chat([Message(role="user", content="hi")]))

    assert response.reasoning_details == [first, vars(second)]
    replay = Message(
        role="assistant", reasoning_details=response.reasoning_details
    ).to_dict()
    assert replay["reasoning_details"] == [first, vars(second)]


def test_openhack_uses_openrouter_reported_cost(monkeypatch):
    monkeypatch.setattr(settings, "openhack_max_retries", 0)
    usage = SimpleNamespace(
        prompt_tokens=100,
        completion_tokens=20,
        model_extra={"cost": 0.0123},
    )
    stream = _Stream([
        _chunk(_delta(content="ok"), finish_reason="stop", usage=usage),
    ])
    llm, _ = _client(stream)
    llm.PRICING = {"requested-model": {"input": 999, "output": 999}}

    response = asyncio.run(llm._chat([Message(role="user", content="hi")]))

    assert response.cost == 0.0123
    assert llm.total_cost == 0.0123


def test_fast_mode_sends_inference_routing_header(monkeypatch):
    monkeypatch.setattr(settings, "openhack_max_retries", 0)
    monkeypatch.setattr(settings, "fast_mode", True)
    stream = _Stream([_chunk(_delta(content="ok"), finish_reason="stop")])
    llm, _ = _client(stream)
    llm.provider = "openhack"
    captured = {}

    async def create(**kwargs):
        captured.update(kwargs)
        return stream

    llm.client.chat.completions.create = create
    asyncio.run(llm._chat([Message(role="user", content="hi")]))

    assert captured["extra_headers"] == {"X-OpenHack-Mode": "fast"}


def test_malformed_tool_arguments_are_preserved_with_parse_error(monkeypatch):
    monkeypatch.setattr(settings, "openhack_max_retries", 0)
    tc = SimpleNamespace(
        index=0,
        id="tool-1",
        function=SimpleNamespace(name="read_file", arguments='{"path":'),
    )
    stream = _Stream([_chunk(_delta(tool_calls=[tc]), finish_reason="tool_calls")])
    llm, events = _client(stream)
    response = asyncio.run(llm._chat([Message(role="user", content="hi")]))

    call = response.tool_calls[0]
    assert call.arguments == {}
    assert call.raw_arguments == '{"path":'
    assert call.parse_error
    failure = next(
        data for kind, data, _ in events if kind == "tool_arguments_parse_failed"
    )
    assert failure["raw_arguments"] == '{"path":'


def test_cancelled_stream_logs_partial_content(monkeypatch):
    monkeypatch.setattr(settings, "openhack_max_retries", 0)
    stream = _Stream(
        [_chunk(_delta(content="partial"))],
        terminal_error=asyncio.CancelledError(),
    )
    llm, events = _client(stream)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(llm._chat([Message(role="user", content="hi")]))

    failed = next(data for kind, data, _ in events if kind == "model_attempt_failed")
    assert failed["partial_content"] == "partial"
    assert any(kind == "model_call_cancelled" for kind, _, _ in events)
