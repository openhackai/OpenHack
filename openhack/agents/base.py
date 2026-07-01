"""
Base agent class for the multi-agent vulnerability scanning system.
"""

import json
import logging
from abc import ABC, abstractmethod
from typing import Any, Optional

import openai

from .llm import LLMClient, Message, ToolCall, ToolResult
from .session import Session
from .context_manager import ContextWindowManager, MODEL_CONTEXT_LIMITS, DEFAULT_CONTEXT_LIMIT
from openhack.tools.registry import ToolRegistry
from openhack.config import settings

logger = logging.getLogger(__name__)


class BaseAgent(ABC):
    """Base class for all scanning agents."""

    name: str = "base"
    description: str = "Base agent"

    def __init__(self, llm: LLMClient, tools: ToolRegistry, session: Session):
        self.llm = llm
        self.tools = tools
        self.session = session
        self.messages: list[Message] = []
        self._instructions_watermark: int = 0

        context_limit = MODEL_CONTEXT_LIMITS.get(llm.model, DEFAULT_CONTEXT_LIMIT)
        self.context_manager = ContextWindowManager(
            context_window_limit=context_limit,
            compaction_threshold=settings.compaction_threshold,
            tool_result_max_lines=settings.tool_result_max_lines,
        )

    @abstractmethod
    def get_system_prompt(self, context: dict) -> str:
        pass

    def get_tools(self) -> list[dict]:
        return self.tools.get_all_tool_definitions()

    def _inject_pending_instructions(self) -> None:
        """Pull any new user instructions from the session and append them to messages."""
        new, version = self.session.get_new_instructions(self._instructions_watermark)
        self._instructions_watermark = version
        for instruction in new:
            self.messages.append(Message(
                role="user",
                content=(
                    f"[USER INSTRUCTION]: {instruction}\n"
                    "Take this into account for the remainder of your analysis."
                ),
            ))

    def _seed_existing_instructions(self) -> None:
        """Inject any instructions that were given before this agent was created."""
        existing = self.session.get_all_instructions()
        self._instructions_watermark = len(existing)
        if existing:
            combined = "\n".join(f"- {inst}" for inst in existing)
            self.messages.append(Message(
                role="user",
                content=(
                    f"[USER INSTRUCTIONS (given earlier in this scan)]:\n{combined}\n"
                    "Take these into account throughout your analysis."
                ),
            ))

    def _estimate_tokens(self, messages: list[Message], system: str) -> int:
        """Rough token estimate: ~4 chars per token for English text."""
        total_chars = len(system)
        for msg in messages:
            if msg.content:
                total_chars += len(msg.content)
            if msg.tool_calls:
                total_chars += len(json.dumps(msg.tool_calls))
        return total_chars // 4

    def _preflight_compact(self, system_prompt: str) -> None:
        """Compact messages before sending to LLM if estimated tokens exceed limit."""
        estimated = self._estimate_tokens(self.messages, system_prompt)
        limit = self.context_manager.context_window_limit

        if estimated > limit * 0.85:
            logger.warning(
                f"[{self.name}] Pre-flight: estimated {estimated} tokens vs {limit} limit — compacting"
            )
            self.messages = self.context_manager.compact_messages(self.messages, keep_recent_turns=3)
            estimated = self._estimate_tokens(self.messages, system_prompt)

            if estimated > limit * 0.85:
                logger.warning(
                    f"[{self.name}] Still {estimated} tokens after normal compaction — emergency compaction"
                )
                self.messages = self.context_manager.emergency_compact(self.messages)

    async def run(self, task: str, context: Optional[dict] = None) -> dict:
        context = context or {}
        self.session.current_agent = self.name

        system_prompt = self.get_system_prompt(context)
        self.messages = [Message(role="user", content=task)]
        self._seed_existing_instructions()

        max_iterations = 50
        iteration = 0

        while iteration < max_iterations:
            if self.session.cancelled:
                break
            # Block here while the session is paused (no-op when running).
            await self.session.wait_if_paused()
            if self.session.cancelled:
                break
            iteration += 1

            self._inject_pending_instructions()

            self._preflight_compact(system_prompt)

            try:
                response = await self.llm.chat(
                    messages=self.messages,
                    tools=self.get_tools(),
                    system=system_prompt,
                )
            except openai.BadRequestError as e:
                err_msg = str(e)
                if "too long" in err_msg or "too many tokens" in err_msg.lower() or "context length" in err_msg:
                    logger.warning(f"[{self.name}] Context overflow on LLM call — compacting and retrying")
                    self.messages = self.context_manager.compact_messages(self.messages, keep_recent_turns=2)
                    estimated = self._estimate_tokens(self.messages, system_prompt)
                    if estimated > self.context_manager.context_window_limit * 0.85:
                        self.messages = self.context_manager.emergency_compact(self.messages)

                    try:
                        response = await self.llm.chat(
                            messages=self.messages,
                            tools=self.get_tools(),
                            system=system_prompt,
                        )
                    except openai.BadRequestError as e2:
                        err_msg2 = str(e2)
                        if "too long" in err_msg2 or "context length" in err_msg2:
                            logger.warning(f"[{self.name}] Still overflowing after emergency compaction — final attempt")
                            self.messages = self.context_manager.emergency_compact(self.messages)
                            response = await self.llm.chat(
                                messages=self.messages,
                                tools=self.get_tools(),
                                system=system_prompt,
                            )
                        else:
                            raise
                else:
                    raise

            self.session.total_cost += response.cost
            if response.usage:
                self.session.total_tokens += response.usage.get("total_tokens", 0)
                self.context_manager.update_usage(response.usage.get("input_tokens", 0))

            if response.content:
                self.session.add_trace(
                    agent=self.name,
                    event_type="thinking",
                    content=response.content,
                )

            if not response.tool_calls:
                return self._parse_final_response(response.content or "")

            assistant_msg = Message(
                role="assistant",
                content=response.content,
                tool_calls=[
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                    }
                    for tc in response.tool_calls
                ],
                reasoning_content=getattr(response, 'reasoning_content', None),
            )
            self.messages.append(assistant_msg)

            for tool_call in response.tool_calls:
                self.session.add_trace(
                    agent=self.name,
                    event_type="tool_call",
                    content=f"Calling {tool_call.name}",
                    tool_name=tool_call.name,
                    tool_input=tool_call.arguments,
                )

                if self.tools.is_async_tool(tool_call.name):
                    result = await self.tools.execute_tool_async(tool_call.name, tool_call.arguments)
                else:
                    # Run sync tools in a worker thread so a long-running tool
                    # (nmap, nuclei, a slow shell command) doesn't block the event
                    # loop — keeps the TUI responsive and the spinner animating.
                    import asyncio as _asyncio
                    result = await _asyncio.to_thread(
                        self.tools.execute_tool, tool_call.name, tool_call.arguments
                    )

                self.session.add_trace(
                    agent=self.name,
                    event_type="tool_result",
                    content=f"Result from {tool_call.name}",
                    tool_name=tool_call.name,
                    tool_output=result,
                )

                raw_content = json.dumps(result) if isinstance(result, dict) else str(result)
                truncated_content = self.context_manager.truncate_tool_result(tool_call.name, raw_content)
                tool_result = ToolResult(
                    tool_call_id=tool_call.id,
                    content=truncated_content,
                )
                self.messages.append(tool_result.to_message())

            if self.context_manager.needs_compaction():
                self.messages = self.context_manager.compact_messages(self.messages)
                logger.info(f"[{self.name}] Compacted message history ({self.context_manager.last_input_tokens} input tokens)")

        return {"error": "Max iterations reached", "partial_result": self.messages[-1].content}

    def _parse_final_response(self, content: str) -> dict:
        return {"response": content}
