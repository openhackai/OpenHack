import asyncio
from types import SimpleNamespace

from openhack import providers
from openhack.agents.llm import LLMClient, Message


class AsyncEvents:
    def __init__(self, events):
        self.events = events

    def __aiter__(self):
        self.iterator = iter(self.events)
        return self

    async def __anext__(self):
        try:
            return next(self.iterator)
        except StopIteration:
            raise StopAsyncIteration


def test_subscription_responses_stream_maps_text_tools_and_usage(monkeypatch):
    resolved = providers.ResolvedProvider(
        name="openai",
        base_url="https://chatgpt.com/backend-api/codex",
        api_key="access",
        model="gpt-5.4",
        supports_prompt_cache=True,
        pricing={},
        auth_type="oauth",
        account_id="acct",
    )
    monkeypatch.setattr(providers, "resolve", lambda *a, **k: resolved)
    client = LLMClient(provider="openai")
    monkeypatch.setattr(
        client, "_ensure_openai_subscription_client", lambda: asyncio.sleep(0)
    )
    captured = {}
    response = SimpleNamespace(
        id="resp",
        model="gpt-5.4",
        usage=SimpleNamespace(input_tokens=10, output_tokens=4),
    )
    events = [
        SimpleNamespace(type="response.created", response=response),
        SimpleNamespace(type="response.output_text.delta", delta="hello"),
        SimpleNamespace(
            type="response.output_item.added",
            output_index=0,
            item=SimpleNamespace(
                type="function_call", id="item", call_id="call", name="read_file"
            ),
        ),
        SimpleNamespace(
            type="response.function_call_arguments.delta",
            item_id="item",
            output_index=0,
            delta='{"path":"a"}',
        ),
        SimpleNamespace(
            type="response.output_item.done",
            item={
                "type": "reasoning",
                "id": "reasoning",
                "encrypted_content": "opaque",
            },
        ),
        SimpleNamespace(type="response.completed", response=response),
    ]

    class Responses:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return AsyncEvents(events)

    client.client = SimpleNamespace(responses=Responses())

    async def run():
        return [
            event
            async for event in client._responses_as_chat_stream(
                [Message(role="user", content="inspect")],
                [
                    {
                        "name": "read_file",
                        "description": "Read",
                        "parameters": {"type": "object", "properties": {}},
                    }
                ],
                "system",
                "required",
            )
        ]

    chunks = asyncio.run(run())
    assert captured["store"] is False
    assert captured["instructions"] == "system"
    assert captured["tool_choice"] == "required"
    assert chunks[0].choices[0].delta.content == "hello"
    assert chunks[2].choices[0].delta.tool_calls[0].function.arguments == '{"path":"a"}'
    assert chunks[-1].usage.prompt_tokens == 10
    assert client._last_response_items[0]["encrypted_content"] == "opaque"


def test_subscription_replays_opaque_response_items(monkeypatch):
    resolved = providers.ResolvedProvider(
        name="openai",
        base_url="https://chatgpt.com/backend-api/codex",
        api_key="access",
        model="gpt-5.4",
        supports_prompt_cache=True,
        pricing={},
        auth_type="oauth",
    )
    monkeypatch.setattr(providers, "resolve", lambda *a, **k: resolved)
    client = LLMClient(provider="openai")
    opaque = {
        "type": "reasoning",
        "id": "reasoning",
        "encrypted_content": "opaque",
    }
    converted = client._convert_messages_to_responses(
        [
            Message(
                role="assistant",
                content="ignored reconstruction",
                response_items=[opaque],
            )
        ]
    )
    assert converted == [opaque]
