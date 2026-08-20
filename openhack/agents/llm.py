"""
LLM client for OpenHack.
"""

import asyncio
import json
import logging
import time
import urllib.request
from uuid import uuid4
from typing import Any, Callable, Optional
from dataclasses import dataclass, field
from types import SimpleNamespace

import openai

from openhack.config import settings

logger = logging.getLogger(__name__)

# Ceiling on a single retry wait — see the backoff site for why.
MAX_RETRY_BACKOFF = 20

# Floor on the stall watchdog. The clock starts once response headers land, so
# it has to cover prefill on a large context (time-to-first-token was 11s+ on a
# ~100k-token turn in session cfeb868f). Anything shorter would kill healthy
# calls, so a misconfigured setting gets clamped up to this.
MIN_STALL_TIMEOUT = 10


def _reported_usage_cost(usage: Any) -> Optional[float]:
    """Read OpenRouter's measured cost from an OpenAI-style usage object."""
    value = getattr(usage, "cost", None)
    if value is None:
        extra = getattr(usage, "model_extra", None)
        if isinstance(extra, dict):
            value = extra.get("cost")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return float(value)
    return None


def _messages_for_event(messages: list[dict]) -> list[dict]:
    """Copy request messages while omitting private reasoning payloads."""
    sanitized = []
    for message in messages:
        logged = dict(message)
        reasoning = logged.pop("reasoning_content", None)
        if reasoning:
            logged["reasoning_characters"] = len(reasoning)
        details = logged.pop("reasoning_details", None)
        if details:
            logged["reasoning_detail_count"] = len(details)
        sanitized.append(logged)
    return sanitized


class StreamStalled(Exception):
    """A stream returned 200 and then stopped making progress.

    The socket read timeout cannot catch this. An upstream that wedges but keeps
    the connection warm — SSE comment keepalives (`: keep-alive`), or a proxy
    that forwards them — resets httpx's read clock on every byte while
    delivering zero decodable events, so the client waits forever. Session
    cfeb868f sat like this for 274s, over half the run, until the user hit Esc.

    So we time *semantic* progress instead: content, reasoning, tool-call
    arguments or usage. Bytes that carry none of those don't count as alive.
    """


@dataclass(frozen=True)
class HostedModelCatalog:
    models: list[dict[str, Any]]
    plan: str
    org_name: str = ""


def fetch_hosted_model_catalog(
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    timeout: float = 10.0,
) -> Optional[HostedModelCatalog]:
    """Fetch model metadata plus the authenticated account's current plan."""
    key = api_key or settings.openhack_api_key
    base = (base_url or settings.openhack_base_url).rstrip("/")
    if not key:
        return None
    try:
        req = urllib.request.Request(
            f"{base}/models",
            headers={"Authorization": f"Bearer {key}", "User-Agent": "openhack"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as e:
        logger.debug(f"fetch_hosted_model_catalog failed: {e}")
        return None
    raw_models = data.get("data", []) if isinstance(data, dict) else []
    models: list[dict[str, Any]] = []
    for raw in raw_models:
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
            continue
        models.append({
            "id": raw["id"],
            "label": str(raw.get("label") or raw.get("name") or raw["id"]),
            "desc": str(raw.get("description") or raw.get("desc") or ""),
            "family": str(raw.get("family") or "OpenHack"),
            "created_at": str(raw.get("created_at") or ""),
            "tab": str(raw.get("tab") or "openhack"),
            "available": raw.get("available") is not False,
            "required_plan": str(raw.get("required_plan") or ""),
        })
    plan = str(data.get("plan") or "").strip().lower() if isinstance(data, dict) else ""
    org_name = str(data.get("org_name") or "").strip() if isinstance(data, dict) else ""
    return HostedModelCatalog(models=models, plan=plan, org_name=org_name)


def fetch_available_model_catalog(
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    timeout: float = 10.0,
) -> Optional[list[dict[str, Any]]]:
    """Fetch the authoritative model catalog served by OpenHack inference.

    The response metadata drives terminal tabs, family sections, labels, and
    release ordering. Returning ``None`` distinguishes a failed refresh from a
    valid empty catalog so callers never substitute speculative local models.
    """
    catalog = fetch_hosted_model_catalog(api_key, base_url, timeout)
    return catalog.models if catalog is not None else None


def fetch_available_models(
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    timeout: float = 10.0,
) -> Optional[list[str]]:
    """Compatibility wrapper returning IDs from the live inference catalog."""
    models = fetch_available_model_catalog(api_key, base_url, timeout)
    return [model["id"] for model in models] if models else None


@dataclass
class Message:
    role: str
    content: Optional[str] = None
    tool_calls: Optional[list[dict]] = None
    tool_call_id: Optional[str] = None
    name: Optional[str] = None
    reasoning_content: Optional[str] = None
    # OpenRouter's normalized reasoning blocks must be replayed unchanged for
    # reasoning models to continue correctly after tool calls.
    reasoning_details: Optional[list[dict]] = None
    # Opaque Responses API output items. Required to continue reasoning/tool
    # turns when store=false; ignored by Chat Completions providers.
    response_items: Optional[list[dict]] = None

    def to_dict(self) -> dict:
        d = {"role": self.role}
        if self.content is not None:
            d["content"] = self.content
        if self.tool_calls is not None:
            d["tool_calls"] = self.tool_calls
        if self.tool_call_id is not None:
            d["tool_call_id"] = self.tool_call_id
        if self.name is not None:
            d["name"] = self.name
        if self.reasoning_content is not None:
            d["reasoning_content"] = self.reasoning_content
        if self.reasoning_details is not None:
            d["reasoning_details"] = self.reasoning_details
        if self.response_items is not None:
            d["response_items"] = self.response_items
        return d


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict
    raw_arguments: str = ""
    parse_error: Optional[str] = None


@dataclass
class ToolResult:
    tool_call_id: str
    content: str

    def to_message(self) -> Message:
        return Message(role="tool", content=self.content, tool_call_id=self.tool_call_id)


@dataclass
class LLMResponse:
    content: Optional[str] = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Optional[dict] = None
    cost: float = 0.0
    reasoning_content: Optional[str] = None
    reasoning_details: list[dict] = field(default_factory=list)
    response_items: list[dict] = field(default_factory=list)
    finish_reason: Optional[str] = None
    response_id: Optional[str] = None
    returned_model: Optional[str] = None
    model_call_id: Optional[str] = None
    attempts: int = 1
    latency_seconds: float = 0.0
    time_to_first_token_seconds: Optional[float] = None


class LLMClient:
    """LLM client for OpenHack."""

    # Client-side cost estimate for the TUI only — the inference layer tracks
    # real cost server-side. Gemma is free on OpenRouter; Mistral/GLM are approx.
    PRICING = {
        "kimi-k2.5": {"input": 0.50, "output": 2.80},
        "glm-5.2": {"input": 1.15, "output": 4.53},
        "gemma-4-31b": {"input": 0.00, "output": 0.00},
        "mistral-large-2512": {"input": 1.60, "output": 4.40},
        "grok-4.5": {"input": 2.00, "output": 6.00},
    }

    # Set to True for the rest of the session when the endpoint rejects
    # prompt_cache_key (e.g. Groq), so we stop sending it.
    _cache_key_unsupported = False

    def __init__(
        self,
        model: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: int = 8192,
        provider: Optional[str] = None,
        prompt_cache_key: Optional[str] = None,
    ):
        self.provider = provider or settings.llm_provider
        # Model resolution is provider-aware: OpenHack uses its configured model,
        # any other provider uses its own default (or an override) — see providers.
        from openhack import providers as _providers
        self._resolved = _providers.resolve(self.provider, model)
        if self._resolved:
            self.model = self._resolved.model
        else:
            self.model = model or settings.openhack_model_id

        self.temperature = temperature
        self.max_tokens = max_tokens
        self.prompt_cache_key = prompt_cache_key
        self.total_cost: float = 0.0
        self.total_tokens: int = 0
        self.total_input_tokens: int = 0
        self.total_output_tokens: int = 0
        # Optional UI hook: status_callback(text) reports transient client state
        # (retry/backoff) to whatever is driving this client. Empty string means
        # "recovered — clear the notice". The TUI wires this to its status line;
        # headless callers leave it unset. Never print() from here — that
        # corrupts a full-screen app's display.
        self.status_callback = None
        # Bound by BaseAgent to Session.record_event. Kept optional so the LLM
        # client remains independently testable.
        self.event_callback = None

        self._init_client()

    def _status(self, text: str) -> None:
        self._event("model_status", {"message": text})
        cb = self.status_callback
        if cb is None:
            return
        try:
            cb(text)
        except Exception:
            pass

    def _permission_denied_message(self, detail: str) -> str:
        """Explain a 403 in terms of the provider that actually returned it.

        OpenAI subscriptions and BYOK providers can use the same OpenAI SDK
        exception types as OpenHack inference.  Treating every ``insufficient``
        response as an OpenHack credit failure sends users to the wrong billing
        page and makes a correctly routed subscription look like it crossed
        provider boundaries.
        """
        detail = str(detail or "permission denied")
        if self.provider == "openhack":
            if "credits" in detail.lower() or "insufficient" in detail.lower():
                return (
                    "Insufficient OpenHack credits. Purchase more at: "
                    f"{settings.openhack_app_url}/settings/billing"
                )
            return (
                f"Access denied by OpenHack API: {detail}\n"
                f"Check your API key at: {settings.openhack_app_url}/settings/api-keys"
            )

        if self.provider == "openai" and self._resolved is not None:
            if self._resolved.auth_type == "oauth":
                return (
                    f"OpenAI subscription denied the request: {detail}\n"
                    "Check your ChatGPT plan or reconnect OpenAI with /connect."
                )
            return (
                f"OpenAI API denied the request: {detail}\n"
                "Check your OpenAI API key, project permissions, and quota."
            )

        provider_name = (
            self._resolved.name if self._resolved is not None else self.provider
        )
        return (
            f"Access denied by {provider_name}: {detail}\n"
            "Check that provider's credentials, permissions, and quota."
        )

    def _authentication_error_message(self, detail: str) -> str:
        """Attribute authentication failures to the selected provider."""
        detail = str(detail or "authentication failed")
        if self.provider == "openhack":
            return (
                f"Authentication failed with OpenHack (401): {detail}\n"
                "Reconnect OpenHack with /connect or check your API key."
            )
        if (
            self.provider == "openai"
            and self._resolved is not None
            and self._resolved.auth_type == "oauth"
        ):
            return (
                f"OpenAI subscription authentication failed (401): {detail}\n"
                "Reconnect OpenAI with /connect."
            )
        provider_name = (
            self._resolved.name if self._resolved is not None else self.provider
        )
        return (
            f"Authentication failed with {provider_name} (401): {detail}\n"
            "Reconnect that provider with /connect or check its API key."
        )

    @staticmethod
    def _is_exhausted_quota_error(error: Exception) -> bool:
        """Return whether retrying this 429 can never succeed without billing."""
        detail = str(getattr(error, "message", error)).lower()
        return any(
            marker in detail
            for marker in (
                "insufficient_quota",
                "credit_balance_exhausted",
                "no credits remaining",
                "insufficient credits",
            )
        )

    def _event(self, event_type: str, data: Any, **correlation: Any) -> None:
        cb = getattr(self, "event_callback", None)
        if cb is None:
            return
        try:
            cb(event_type, data, **correlation)
        except Exception:
            pass

    def _init_client(self):
        # Non-OpenHack provider (bring-your-own-key): resolve from the registry.
        if self._resolved is not None:
            r = self._resolved
            if r.missing_key_env:
                raise ValueError(
                    f"{r.name} is selected as the LLM provider but {r.missing_key_env} "
                    f"is not set.\nExport your key, e.g.:  export {r.missing_key_env}=...\n"
                    f"Or switch back to OpenHack:  /config llm_provider openhack"
                )
            default_headers = None
            if r.auth_type == "oauth":
                default_headers = {"originator": "openhack"}
                if r.account_id:
                    default_headers["ChatGPT-Account-Id"] = r.account_id
            self.client = openai.AsyncOpenAI(
                api_key=r.api_key,
                base_url=r.base_url,
                timeout=settings.openhack_read_timeout,
                max_retries=0,
                default_headers=default_headers,
            )
            return

        # Default: the OpenHack hosted provider.
        if not settings.openhack_api_key:
            raise ValueError(
                "OPENHACK_API_KEY is required.\n"
                f"Sign up at: {settings.openhack_app_url}/signup\n"
                "Then run: openhack /setup"
            )
        self.client = openai.AsyncOpenAI(
            api_key=settings.openhack_api_key,
            base_url=settings.openhack_base_url,
            timeout=settings.openhack_read_timeout,
            max_retries=0,
        )

    def _calculate_cost(self, input_tokens: int, output_tokens: int) -> float:
        if self._resolved is not None:
            # Bring-your-own-key providers: use their pricing if we know it,
            # otherwise report 0 rather than guessing with OpenHack's rates
            # (the user is billed by their own provider anyway).
            pricing = self._resolved.pricing.get(self.model, {"input": 0.0, "output": 0.0})
        else:
            pricing = self.PRICING.get(self.model, {"input": 0.50, "output": 2.80})
        return (input_tokens / 1_000_000) * pricing["input"] + (output_tokens / 1_000_000) * pricing["output"]

    def _convert_tools_to_openai_format(self, tools: list[dict]) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("parameters", {"type": "object", "properties": {}}),
                },
            }
            for tool in tools
        ]

    def _convert_messages_to_openai(self, messages: list[Message], system: Optional[str]) -> list[dict]:
        openai_messages = []
        if system:
            openai_messages.append({"role": "system", "content": system})

        for msg in messages:
            if msg.role == "system":
                openai_messages.append({"role": "system", "content": msg.content or ""})
            elif msg.role == "tool":
                openai_messages.append({
                    "role": "tool",
                    "tool_call_id": msg.tool_call_id,
                    "content": msg.content or "",
                })
            elif msg.role == "assistant" and msg.tool_calls:
                openai_messages.append({
                    "role": "assistant",
                    "content": msg.content,
                    "tool_calls": msg.tool_calls,
                })
            else:
                openai_messages.append({"role": msg.role, "content": msg.content or ""})

        return openai_messages

    def _convert_messages_to_responses(
        self, messages: list[Message]
    ) -> list[dict[str, Any]]:
        """Translate Chat Completions history to Responses input items."""
        result: list[dict[str, Any]] = []
        for message in messages:
            if message.role == "system":
                # System content is folded into `instructions` by the caller.
                continue
            if message.role == "tool":
                result.append(
                    {
                        "type": "function_call_output",
                        "call_id": message.tool_call_id or "",
                        "output": message.content or "",
                    }
                )
                continue
            if message.role == "assistant" and message.response_items:
                result.extend(message.response_items)
                continue
            if message.role == "assistant" and message.tool_calls:
                if message.content:
                    result.append({"role": "assistant", "content": message.content})
                for call in message.tool_calls:
                    fn = call.get("function") or {}
                    result.append(
                        {
                            "type": "function_call",
                            "call_id": call.get("id") or "",
                            "name": fn.get("name") or "",
                            "arguments": fn.get("arguments") or "{}",
                        }
                    )
                continue
            result.append(
                {
                    "role": "assistant" if message.role == "assistant" else "user",
                    "content": message.content or "",
                }
            )
        return result

    def _convert_tools_to_responses_format(self, tools: list[dict]) -> list[dict]:
        return [
            {
                "type": "function",
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get(
                    "parameters", {"type": "object", "properties": {}}
                ),
            }
            for tool in tools
        ]

    async def _ensure_openai_subscription_client(self) -> None:
        """Refresh a ChatGPT OAuth token and rebuild the SDK client if needed."""
        if not self._resolved or self._resolved.auth_type != "oauth":
            return
        from openhack.provider_auth import (
            OPENAI_CODEX_BASE_URL,
            get_openai_oauth,
            refresh_openai_credential,
        )

        credential = get_openai_oauth()
        if credential is None:
            raise ValueError("OpenAI subscription is disconnected. Run /connect openai.")
        refreshed = await asyncio.to_thread(refresh_openai_credential, credential)
        self._resolved.api_key = refreshed.access
        self._resolved.account_id = refreshed.account_id
        headers = {"originator": "openhack"}
        if refreshed.account_id:
            headers["ChatGPT-Account-Id"] = refreshed.account_id
        self.client = openai.AsyncOpenAI(
            api_key=refreshed.access,
            base_url=OPENAI_CODEX_BASE_URL,
            timeout=settings.openhack_read_timeout,
            max_retries=0,
            default_headers=headers,
        )

    async def _responses_as_chat_stream(
        self,
        messages: list[Message],
        tools: Optional[list[dict]],
        system: Optional[str],
        tool_choice: Optional[str],
    ):
        """Expose a Responses API stream through the existing chat-chunk parser."""
        await self._ensure_openai_subscription_client()
        instructions = "\n\n".join(
            part
            for part in [
                system or "",
                *[
                    message.content or ""
                    for message in messages
                    if message.role == "system"
                ],
            ]
            if part
        )
        kwargs: dict[str, Any] = {
            "model": self.model,
            "input": self._convert_messages_to_responses(messages),
            "stream": True,
            "store": False,
            "include": ["reasoning.encrypted_content"],
        }
        if instructions:
            kwargs["instructions"] = instructions
        if tools:
            kwargs["tools"] = self._convert_tools_to_responses_format(tools)
            kwargs["tool_choice"] = tool_choice or "auto"

        response_stream = await self.client.responses.create(**kwargs)
        self._last_response_items: list[dict[str, Any]] = []
        call_indexes: dict[str, int] = {}
        call_meta: dict[int, tuple[str, str]] = {}
        next_index = 0
        response_id = None
        returned_model = None

        async for event in response_stream:
            event_type = getattr(event, "type", "")
            response = getattr(event, "response", None)
            if response is not None:
                response_id = getattr(response, "id", None) or response_id
                returned_model = getattr(response, "model", None) or returned_model

            if event_type == "response.output_text.delta":
                yield SimpleNamespace(
                    id=response_id,
                    model=returned_model,
                    usage=None,
                    choices=[
                        SimpleNamespace(
                            finish_reason=None,
                            delta=SimpleNamespace(
                                content=getattr(event, "delta", "") or "",
                                reasoning_content=None,
                                tool_calls=None,
                            ),
                        )
                    ],
                )
                continue

            if event_type == "response.output_item.added":
                item = getattr(event, "item", None)
                if getattr(item, "type", "") == "function_call":
                    key = getattr(item, "id", None) or getattr(item, "call_id", None) or str(next_index)
                    index = int(getattr(event, "output_index", next_index))
                    next_index = max(next_index, index + 1)
                    call_indexes[str(key)] = index
                    call_meta[index] = (
                        getattr(item, "call_id", "") or "",
                        getattr(item, "name", "") or "",
                    )
                    call_id, name = call_meta[index]
                    yield SimpleNamespace(
                        id=response_id,
                        model=returned_model,
                        usage=None,
                        choices=[
                            SimpleNamespace(
                                finish_reason=None,
                                delta=SimpleNamespace(
                                    content=None,
                                    reasoning_content=None,
                                    tool_calls=[
                                        SimpleNamespace(
                                            index=index,
                                            id=call_id,
                                            function=SimpleNamespace(
                                                name=name, arguments=None
                                            ),
                                        )
                                    ],
                                ),
                            )
                        ],
                    )
                continue

            if event_type == "response.output_item.done":
                item = getattr(event, "item", None)
                if item is not None:
                    if hasattr(item, "model_dump"):
                        dumped = item.model_dump(exclude_none=True)
                    elif isinstance(item, dict):
                        dumped = dict(item)
                    else:
                        dumped = {
                            key: value
                            for key, value in vars(item).items()
                            if value is not None
                        }
                    if isinstance(dumped, dict):
                        self._last_response_items.append(dumped)
                continue

            if event_type == "response.function_call_arguments.delta":
                key = str(
                    getattr(event, "item_id", None)
                    or getattr(event, "call_id", None)
                    or ""
                )
                index = call_indexes.get(
                    key, int(getattr(event, "output_index", next_index))
                )
                if index not in call_meta:
                    call_meta[index] = (
                        getattr(event, "call_id", "") or "",
                        getattr(event, "name", "") or "",
                    )
                call_id, name = call_meta[index]
                yield SimpleNamespace(
                    id=response_id,
                    model=returned_model,
                    usage=None,
                    choices=[
                        SimpleNamespace(
                            finish_reason=None,
                            delta=SimpleNamespace(
                                content=None,
                                reasoning_content=None,
                                tool_calls=[
                                    SimpleNamespace(
                                        index=index,
                                        id=call_id,
                                        function=SimpleNamespace(
                                            name=name,
                                            arguments=getattr(event, "delta", "") or "",
                                        ),
                                    )
                                ],
                            ),
                        )
                    ],
                )
                continue

            if event_type in ("response.completed", "response.incomplete"):
                usage = getattr(response, "usage", None)
                input_tokens = getattr(usage, "input_tokens", 0) if usage else 0
                output_tokens = getattr(usage, "output_tokens", 0) if usage else 0
                finish = "length" if event_type == "response.incomplete" else "stop"
                yield SimpleNamespace(
                    id=response_id,
                    model=returned_model,
                    usage=SimpleNamespace(
                        prompt_tokens=input_tokens,
                        completion_tokens=output_tokens,
                    ),
                    choices=[
                        SimpleNamespace(
                            finish_reason=finish,
                            delta=SimpleNamespace(
                                content=None,
                                reasoning_content=None,
                                tool_calls=None,
                            ),
                        )
                    ],
                )

    async def chat(
        self,
        messages: list[Message],
        tools: Optional[list[dict]] = None,
        system: Optional[str] = None,
        tool_choice: Optional[str] = None,
        on_chunk: Optional[Callable] = None,
    ) -> LLMResponse:
        try:
            return await self._chat(messages, tools, system, tool_choice=tool_choice, on_chunk=on_chunk)
        except openai.APIStatusError as e:
            detail = str(e).lower()
            if (
                self.prompt_cache_key
                and not LLMClient._cache_key_unsupported
                and ("prompt_cache_key" in detail or "prompt cache key" in detail)
            ):
                LLMClient._cache_key_unsupported = True
                # NEVER print here: in the TUI this writes straight into the
                # full-screen buffer, tearing the layout and leaving text the
                # renderer doesn't know about (it survives until that region
                # happens to be redrawn). Log it, and tell the UI via the hook.
                logger.warning(
                    "Endpoint doesn't support prompt caching — retrying without it. "
                    "To disable permanently: /config prompt_caching false"
                )
                self._status("endpoint rejected prompt caching — retrying without it")
                return await self._chat(messages, tools, system, tool_choice=tool_choice, on_chunk=on_chunk)
            raise

    async def _chat(
        self,
        messages: list[Message],
        tools: Optional[list[dict]] = None,
        system: Optional[str] = None,
        tool_choice: Optional[str] = None,
        on_chunk: Optional[Callable] = None,
    ) -> LLMResponse:
        model_call_id = str(uuid4())
        call_started = time.monotonic()
        first_token_at: Optional[float] = None
        openai_messages = self._convert_messages_to_openai(messages, system)

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": openai_messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        # OpenHack inference translates this trusted product hint into
        # OpenRouter's provider.sort="throughput" routing. It is deliberately
        # a header so it never leaks as an unsupported model parameter.
        from openhack import config as _config
        if self._resolved is None and _config.settings.fast_mode:
            kwargs["extra_headers"] = {"X-OpenHack-Mode": "fast"}
        if tools:
            kwargs["tools"] = self._convert_tools_to_openai_format(tools)
            kwargs["tool_choice"] = tool_choice or "auto"
        # Re-read settings via the module so /config changes apply mid-session.
        # OpenHack and OpenAI accept prompt_cache_key; other OpenAI-compatible
        # endpoints often reject unknown params, so only send it when supported.
        provider_supports_cache = self._resolved is None or self._resolved.supports_prompt_cache
        if (
            self.prompt_cache_key
            and provider_supports_cache
            and _config.settings.prompt_caching
            and not LLMClient._cache_key_unsupported
        ):
            kwargs["prompt_cache_key"] = self.prompt_cache_key

        max_retries = settings.openhack_max_retries
        last_exception = None
        self._event(
            "model_call_started",
            {
                "provider": getattr(self, "provider", None),
                "requested_model": self.model,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
                "stream": True,
                "tool_choice": tool_choice or ("auto" if tools else None),
                "messages": _messages_for_event(openai_messages),
                "tools": self._convert_tools_to_openai_format(tools) if tools else [],
            },
            model_call_id=model_call_id,
        )

        for attempt in range(max_retries + 1):
            stream = None
            content_parts: list[str] = []
            reasoning_parts: list[str] = []
            reasoning_details: list[dict] = []
            tool_call_acc: dict[int, dict] = {}

            def record_failure(exc: BaseException, retryable: bool) -> None:
                self._event(
                    "model_attempt_failed",
                    {
                        "attempt": attempt + 1,
                        "max_attempts": max_retries + 1,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "retryable": retryable,
                        "will_retry": retryable and attempt < max_retries,
                        "partial_content": "".join(content_parts),
                        "partial_reasoning_characters": sum(
                            len(part) for part in reasoning_parts
                        ),
                        "partial_tool_calls": [
                            {
                                "index": idx,
                                "id": acc.get("id"),
                                "name": acc.get("name"),
                                "raw_arguments": "".join(
                                    acc.get("arguments_parts") or []
                                ),
                            }
                            for idx, acc in sorted(tool_call_acc.items())
                        ],
                        "elapsed_seconds": time.monotonic() - call_started,
                    },
                    model_call_id=model_call_id,
                )

            try:
                if attempt > 0:
                    # Capped exponential backoff. Uncapped this went
                    # 5→10→20→40→80 = 155s of dead air across the retry chain,
                    # which is most of what "it got stuck" actually was.
                    wait_time = min(5 * (2 ** (attempt - 1)), MAX_RETRY_BACKOFF)
                    reason = type(last_exception).__name__ if last_exception else "error"
                    logger.warning(
                        f"Retrying API call (attempt {attempt + 1}/{max_retries + 1}) "
                        f"after {wait_time}s — {reason}"
                    )
                    # Surface the wait in the UI instead of printing over it, so a
                    # long backoff reads as "retrying", not as a frozen app.
                    self._status(
                        f"upstream {reason} — retrying in {wait_time}s "
                        f"({attempt + 1}/{max_retries + 1})"
                    )
                    self._event(
                        "model_call_retry_scheduled",
                        {
                            "attempt": attempt + 1,
                            "max_attempts": max_retries + 1,
                            "wait_seconds": wait_time,
                            "reason": reason,
                            "error": str(last_exception) if last_exception else None,
                        },
                        model_call_id=model_call_id,
                    )
                    await asyncio.sleep(wait_time)

                if self._resolved and self._resolved.auth_type == "oauth":
                    stream = self._responses_as_chat_stream(
                        messages, tools, system, tool_choice
                    )
                else:
                    stream = await self.client.chat.completions.create(**kwargs)
                self._event(
                    "model_stream_opened",
                    {"attempt": attempt + 1},
                    model_call_id=model_call_id,
                )
                # Recovered: clear the retry notice so it can't linger after the
                # call succeeds.
                if attempt > 0:
                    self._status("")

                input_tokens = 0
                output_tokens = 0
                reported_cost: Optional[float] = None
                finish_reason: Optional[str] = None
                response_id: Optional[str] = None
                returned_model: Optional[str] = None

                # Stall watchdog. Iterate by hand rather than `async for` so each
                # step carries a deadline measured from the last *meaningful*
                # delta — see StreamStalled for why byte-level timeouts miss this.
                stall_limit = max(
                    MIN_STALL_TIMEOUT, _config.settings.openhack_stream_stall_timeout
                )
                stream_iter = stream.__aiter__()
                last_progress = time.monotonic()

                while True:
                    remaining = stall_limit - (time.monotonic() - last_progress)
                    if remaining <= 0:
                        raise StreamStalled(
                            f"no output for {stall_limit}s (stream open but idle)"
                        )
                    try:
                        chunk = await asyncio.wait_for(
                            stream_iter.__anext__(), timeout=remaining
                        )
                    except StopAsyncIteration:
                        break
                    except (asyncio.TimeoutError, TimeoutError) as e:
                        raise StreamStalled(
                            f"no output for {stall_limit}s (stream open but idle)"
                        ) from e

                    progressed = False
                    response_id = getattr(chunk, "id", None) or response_id
                    returned_model = getattr(chunk, "model", None) or returned_model

                    if chunk.usage:
                        input_tokens = chunk.usage.prompt_tokens or 0
                        output_tokens = chunk.usage.completion_tokens or 0
                        measured = _reported_usage_cost(chunk.usage)
                        if measured is not None:
                            reported_cost = measured
                        progressed = True

                    if not chunk.choices:
                        if progressed:
                            last_progress = time.monotonic()
                        continue

                    delta = chunk.choices[0].delta
                    chunk_finish = getattr(chunk.choices[0], "finish_reason", None)
                    if chunk_finish is not None:
                        finish_reason = str(chunk_finish)
                        self._event(
                            "model_finish_reason_received",
                            {"finish_reason": finish_reason, "attempt": attempt + 1},
                            model_call_id=model_call_id,
                        )

                    if delta.content:
                        if first_token_at is None:
                            first_token_at = time.monotonic()
                        content_parts.append(delta.content)
                        progressed = True
                        self._event(
                            "model_stream_delta",
                            {"kind": "content", "delta": delta.content},
                            model_call_id=model_call_id,
                            durable=False,
                        )
                        if on_chunk:
                            on_chunk("content", delta.content)

                    rc = getattr(delta, "reasoning_content", None)
                    if rc:
                        if first_token_at is None:
                            first_token_at = time.monotonic()
                        reasoning_parts.append(rc)
                        progressed = True
                        # Do not put private chain-of-thought in an operational
                        # log. Presence and size are enough to diagnose stalls.
                        self._event(
                            "model_stream_delta",
                            {"kind": "reasoning", "characters": len(rc)},
                            model_call_id=model_call_id,
                            durable=False,
                        )
                        if on_chunk:
                            on_chunk("reasoning", rc)

                    # OpenRouter emits one or more normalized reasoning blocks
                    # per streaming chunk. Its contract is to concatenate the
                    # blocks in order and replay the complete sequence on the
                    # assistant message in the next request.
                    detail_chunks = getattr(delta, "reasoning_details", None)
                    if detail_chunks:
                        for detail in detail_chunks:
                            if isinstance(detail, dict):
                                dumped = detail
                            elif hasattr(detail, "model_dump"):
                                dumped = detail.model_dump(exclude_none=True)
                            else:
                                dumped = dict(vars(detail))
                            reasoning_details.append(dumped)
                        progressed = True

                    if delta.tool_calls:
                        for tc_delta in delta.tool_calls:
                            idx = tc_delta.index
                            if idx not in tool_call_acc:
                                tool_call_acc[idx] = {
                                    "id": tc_delta.id or "",
                                    "name": (tc_delta.function.name if tc_delta.function else "") or "",
                                    "arguments_parts": [],
                                }
                            acc = tool_call_acc[idx]
                            if tc_delta.id:
                                acc["id"] = tc_delta.id
                            if tc_delta.function:
                                if tc_delta.function.name:
                                    if first_token_at is None:
                                        first_token_at = time.monotonic()
                                    acc["name"] = tc_delta.function.name
                                    progressed = True
                                if tc_delta.function.arguments:
                                    acc["arguments_parts"].append(tc_delta.function.arguments)
                                    progressed = True
                                    if first_token_at is None:
                                        first_token_at = time.monotonic()
                                    self._event(
                                        "model_stream_delta",
                                        {
                                            "kind": "tool_args",
                                            "tool_index": idx,
                                            "tool_call_id": acc["id"],
                                            "tool_name": acc["name"],
                                            "delta": tc_delta.function.arguments,
                                        },
                                        model_call_id=model_call_id,
                                        tool_call_id=acc["id"] or None,
                                        durable=False,
                                    )
                                    # Writing a large file means minutes of pure
                                    # tool-argument stream. Without this the UI
                                    # gets no signal at all and looks hung.
                                    if on_chunk:
                                        on_chunk("tool_args", tc_delta.function.arguments)

                    if progressed:
                        last_progress = time.monotonic()

                content = "".join(content_parts) or None
                reasoning_content = "".join(reasoning_parts) or None

                tool_calls = []
                for idx in sorted(tool_call_acc.keys()):
                    acc = tool_call_acc[idx]
                    raw_args = "".join(acc["arguments_parts"])
                    try:
                        args = json.loads(raw_args) if raw_args else {}
                        parse_error = None
                    except json.JSONDecodeError as exc:
                        logger.warning(f"Failed to parse tool call arguments: {raw_args[:200]}")
                        args = {}
                        parse_error = str(exc)
                        self._event(
                            "tool_arguments_parse_failed",
                            {
                                "tool_index": idx,
                                "tool_name": acc["name"],
                                "raw_arguments": raw_args,
                                "error": parse_error,
                            },
                            model_call_id=model_call_id,
                            tool_call_id=acc["id"] or None,
                        )
                    tool_calls.append(
                        ToolCall(
                            id=acc["id"],
                            name=acc["name"],
                            arguments=args,
                            raw_arguments=raw_args,
                            parse_error=parse_error,
                        )
                    )

                if input_tokens == 0 and output_tokens == 0:
                    logger.debug("No usage data in stream — cost will be zero for this call")

                # Hosted inference is routed exclusively through OpenRouter;
                # trust its measured usage.cost rather than maintaining a
                # second pricing table in the scanner. BYOK providers retain
                # their local catalog estimate because OpenRouter is not in
                # that request path.
                cost = (
                    reported_cost or 0.0
                    if self._resolved is None
                    else self._calculate_cost(input_tokens, output_tokens)
                )
                self.total_cost += cost
                self.total_tokens += input_tokens + output_tokens
                self.total_input_tokens += input_tokens
                self.total_output_tokens += output_tokens

                llm_response = LLMResponse(
                    content=content,
                    tool_calls=tool_calls,
                    usage={"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": input_tokens + output_tokens},
                    cost=cost,
                    finish_reason=finish_reason,
                    response_id=response_id,
                    returned_model=returned_model,
                    model_call_id=model_call_id,
                    attempts=attempt + 1,
                    latency_seconds=time.monotonic() - call_started,
                    time_to_first_token_seconds=(
                        first_token_at - call_started if first_token_at else None
                    ),
                    response_items=(
                        list(getattr(self, "_last_response_items", []))
                        if self._resolved and self._resolved.auth_type == "oauth"
                        else []
                    ),
                    reasoning_details=reasoning_details,
                )
                llm_response.reasoning_content = reasoning_content
                self._event(
                    "model_call_completed",
                    {
                        "finish_reason": finish_reason,
                        "response_id": response_id,
                        "returned_model": returned_model,
                        "attempts": attempt + 1,
                        "latency_seconds": llm_response.latency_seconds,
                        "time_to_first_token_seconds": llm_response.time_to_first_token_seconds,
                        "usage": llm_response.usage,
                        "cost": cost,
                        "content": content,
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "name": tc.name,
                                "arguments": tc.arguments,
                                "raw_arguments": tc.raw_arguments,
                                "parse_error": tc.parse_error,
                            }
                            for tc in tool_calls
                        ],
                    },
                    model_call_id=model_call_id,
                )
                return llm_response

            except openai.RateLimitError as e:
                if stream:
                    try: await stream.close()
                    except Exception: pass
                if self._is_exhausted_quota_error(e):
                    record_failure(e, False)
                    detail = getattr(e, "message", str(e))
                    raise ValueError(self._permission_denied_message(detail)) from e
                record_failure(e, True)
                last_exception = e
                if attempt == max_retries:
                    raise
            except openai.AuthenticationError as e:
                record_failure(e, False)
                detail = getattr(e, "message", str(e))
                raise ValueError(self._authentication_error_message(detail)) from e
            except openai.PermissionDeniedError as e:
                record_failure(e, False)
                detail = getattr(e, "message", str(e))
                raise ValueError(self._permission_denied_message(detail)) from e
            except openai.APIStatusError as e:
                record_failure(e, e.status_code >= 500)
                if stream:
                    try: await stream.close()
                    except Exception: pass
                if e.status_code >= 500:
                    last_exception = e
                    if attempt == max_retries:
                        raise
                else:
                    raise
            except openai.APITimeoutError as e:
                record_failure(e, True)
                last_exception = e
                if stream:
                    try: await stream.close()
                    except Exception: pass
                if attempt == max_retries:
                    raise
            except openai.APIConnectionError as e:
                record_failure(e, True)
                last_exception = e
                if stream:
                    try: await stream.close()
                    except Exception: pass
                if attempt == max_retries:
                    raise
                continue
            except openai.APIError as e:
                record_failure(e, True)
                # Base APIError not matched by the specific handlers above — most
                # commonly a transient upstream error delivered *mid-stream* as an
                # SSE error event (e.g. a provider/gateway 'server_error' like
                # AtlasCloud's "服务发生异常，请重试") rather than a 5xx HTTP status.
                # Treat it as transient and retry with backoff instead of failing
                # the whole turn. (Auth/permission/4xx are raised above already.)
                last_exception = e
                if stream:
                    try: await stream.close()
                    except Exception: pass
                if attempt == max_retries:
                    raise
            except StreamStalled as e:
                record_failure(e, True)
                # A wedged-but-warm upstream. Closing the stream drops the
                # connection, so the retry gets a fresh one — and, at the
                # gateway, another shot at provider failover.
                last_exception = e
                logger.warning(f"Stream stalled — abandoning and retrying: {e}")
                self._status(f"upstream went quiet — reconnecting ({e})")
                if stream:
                    try: await stream.close()
                    except Exception: pass
                if attempt == max_retries:
                    raise
            except asyncio.CancelledError as e:
                record_failure(e, False)
                self._event(
                    "model_call_cancelled",
                    {"attempt": attempt + 1},
                    model_call_id=model_call_id,
                )
                if stream:
                    try:
                        await stream.close()
                    except Exception:
                        pass
                raise
            except Exception as e:
                record_failure(e, False)
                logger.debug(f"OpenHack API error: {e}", exc_info=True)
                if stream:
                    try: await stream.close()
                    except Exception: pass
                raise

        if last_exception:
            raise last_exception
