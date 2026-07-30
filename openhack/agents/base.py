"""
Base agent class for the multi-agent vulnerability scanning system.
"""

import json
import logging
import hashlib
import time
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
    requires_finish_task: bool = False

    def __init__(self, llm: LLMClient, tools: ToolRegistry, session: Session):
        self.llm = llm
        self.tools = tools
        self.session = session
        self.llm.event_callback = session.record_event
        self.messages: list[Message] = []
        self._instructions_watermark: int = 0
        # Optional per-token stream hook: stream_callback(kind, delta) where kind
        # is "content" or "reasoning". Set by a UI to render tokens live.
        self.stream_callback = None
        # Durable working memory that survives compaction (fixes the thrash
        # death-spiral): a compact ledger of every action taken + its result,
        # re-injected each turn; a cache of calls already made (anti-repetition);
        # and a counter of consecutive no-new-progress turns (progress-aware stop).
        self._ledger: list[str] = []
        self._call_cache: dict[str, tuple[int, str]] = {}
        self._stale_turns: int = 0
        # Result summaries seen so far. Real thrash varies one arg each attempt so
        # exact-call dedup never fires, but the *results* stop changing — no novel
        # signal. Tracking result novelty is what actually catches near-dup thrash.
        self._seen_summaries: set[str] = set()

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

    def _stream_chunk(self, kind: str, delta: str) -> None:
        """Forward streamed tokens to the UI hook, if one is attached."""
        cb = self.stream_callback
        if cb is not None:
            try:
                cb(kind, delta)
            except Exception:
                pass

    def _inject_pending_instructions(self) -> None:
        """Pull any new user instructions from the session and append them to messages."""
        new, version = self.session.get_new_instructions(self._instructions_watermark)
        self._instructions_watermark = version
        for instruction in new:
            self._append_message(Message(
                role="user",
                content=(
                    f"[USER INSTRUCTION]: {instruction}\n"
                    "Take this into account for the remainder of your analysis."
                ),
            ), source="user_instruction")

    def _seed_existing_instructions(self) -> None:
        """Inject any instructions that were given before this agent was created."""
        existing = self.session.get_all_instructions()
        self._instructions_watermark = len(existing)
        if existing:
            combined = "\n".join(f"- {inst}" for inst in existing)
            self._append_message(Message(
                role="user",
                content=(
                    f"[USER INSTRUCTIONS (given earlier in this scan)]:\n{combined}\n"
                    "Take these into account throughout your analysis."
                ),
            ), source="seeded_instruction")

    def _append_message(self, message: Message, *, source: str) -> None:
        self.messages.append(message)
        logged_message = message.to_dict()
        reasoning = logged_message.pop("reasoning_content", None)
        if reasoning:
            logged_message["reasoning_characters"] = len(reasoning)
        self.session.record_event(
            "message_appended",
            {
                "index": len(self.messages) - 1,
                "source": source,
                "message": logged_message,
            },
            agent=self.name,
        )

    def _last_assistant_content(self) -> str:
        for message in reversed(self.messages):
            if message.role == "assistant" and message.content:
                return message.content
        return ""

    @staticmethod
    def _looks_like_unfinished_promise(text: str) -> bool:
        """Whether the tail says work will happen after this response."""
        tail = " ".join((text or "").lower().split())[-280:]
        return any(marker in tail for marker in (
            "let me ",
            "i'll ",
            "i will ",
            "next i'll ",
            "next i will ",
            "i'm going to ",
            "i am going to ",
        ))

    @classmethod
    def _operator_answer_for_completion(
        cls,
        *,
        summary: str,
        reason: str,
        response_content: Optional[str],
        guarded_content: Optional[str],
    ) -> str:
        """Choose user-facing prose over a shorter lifecycle recap.

        Providers sometimes answer normally, receive the continuation guard,
        then call finish_task with a third-person synopsis. Preserve a
        substantive completed answer, but never let a forward-looking promise
        masquerade as completion.
        """
        natural = (response_content or "").strip() or (guarded_content or "").strip()
        if not natural or cls._looks_like_unfinished_promise(natural):
            return summary
        if reason in {"no_action_needed", "needs_user_input"}:
            return natural
        if len(natural) > len(summary) * 1.2:
            return natural
        return summary

    def _record_response_usage(self, response) -> None:
        self.session.total_cost += response.cost
        if not response.usage:
            return
        self.session.total_tokens += response.usage.get("total_tokens", 0)
        self.session.total_input_tokens += response.usage.get("input_tokens", 0)
        self.session.total_output_tokens += response.usage.get("output_tokens", 0)
        self.context_manager.update_usage(response.usage.get("input_tokens", 0))

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
            before = [m.to_dict() for m in self.messages]
            self.messages = self.context_manager.compact_messages(self.messages, keep_recent_turns=3)
            self.session.record_event(
                "context_compacted",
                {
                    "mode": "preflight",
                    "estimated_tokens_before": estimated,
                    "limit": limit,
                    "messages_before": before,
                    "messages_after": [m.to_dict() for m in self.messages],
                },
                agent=self.name,
            )
            estimated = self._estimate_tokens(self.messages, system_prompt)

            if estimated > limit * 0.85:
                logger.warning(
                    f"[{self.name}] Still {estimated} tokens after normal compaction — emergency compaction"
                )
                before = [m.to_dict() for m in self.messages]
                self.messages = self.context_manager.emergency_compact(self.messages)
                self.session.record_event(
                    "context_compacted",
                    {
                        "mode": "preflight_emergency",
                        "estimated_tokens_before": estimated,
                        "limit": limit,
                        "messages_before": before,
                        "messages_after": [m.to_dict() for m in self.messages],
                    },
                    agent=self.name,
                )

    async def run(self, task: str, context: Optional[dict] = None) -> dict:
        context = context or {}
        self.session.current_agent = self.name
        self.session.transition_status("running", "agent_turn_started")
        self.session.start_turn(task, self.name)

        self._system_prompt = self.get_system_prompt(context)
        self.messages = []
        self._append_message(Message(role="user", content=task), source="run")
        self._ledger = []
        self._call_cache = {}
        self._stale_turns = 0
        self._seen_summaries = set()
        self._seed_existing_instructions()
        self.session.record_event(
            "agent_configuration",
            {
                "agent": self.name,
                "model": self.llm.model,
                "provider": getattr(self.llm, "provider", None),
                "system_prompt_sha256": hashlib.sha256(
                    self._system_prompt.encode("utf-8")
                ).hexdigest(),
                "system_prompt": self._system_prompt,
                "tools": self.get_tools(),
                "requires_finish_task": self.requires_finish_task,
            },
            agent=self.name,
        )
        return await self._agent_loop()

    # ── Durable working memory (anti-thrash) ──────────────────────────
    @staticmethod
    def _call_key(name: str, args: dict) -> str:
        try:
            a = json.dumps(args, sort_keys=True)
        except Exception:
            a = str(args)
        return f"{name}::{a}"

    @staticmethod
    def _arg_hint(args: dict) -> str:
        if not isinstance(args, dict):
            return ""
        for k in ("command", "query", "url", "payload", "target", "path", "to",
                  "marker", "pattern", "domain", "name", "shell_id"):
            v = args.get(k)
            if isinstance(v, str) and v:
                return v if len(v) <= 90 else v[:87] + "…"
        try:
            s = json.dumps(args)
        except Exception:
            s = str(args)
        return s if len(s) <= 90 else s[:87] + "…"

    @staticmethod
    def _result_summary(result) -> str:
        if isinstance(result, dict):
            if "error" in result:
                return f"error: {str(result['error'])[:90]}"
            if "exit_code" in result:
                out = (str(result.get("stdout", "")) or "").strip().replace("\n", " ")
                return f"exit {result['exit_code']}" + (f" · {out[:90]}" if out else "")
            for k in ("injectable", "fired", "installed", "count", "interactions", "status", "vulnerable_packages"):
                if k in result:
                    return f"{k}={result[k]}"
        s = str(result).strip().replace("\n", " ")
        return s[:110] + ("…" if len(s) > 110 else "")

    def _ledger_block(self) -> str:
        """A pinned record of actions taken — injected every turn, never compacted."""
        if not self._ledger:
            return ""
        recent = self._ledger[-45:]
        return (
            "\n\n## Actions already taken (do NOT repeat these — build on what you learned):\n"
            + "\n".join(recent)
            + "\nIf you find yourself about to repeat an action above, do something different instead."
        )

    async def continue_run(self, task: str) -> dict:
        """Continue an existing conversation with a new user turn.

        Unlike run(), this keeps the prior message history and system prompt, so
        the agent remembers what it just did — used for interactive follow-ups.
        """
        # A new user-initiated turn clears any prior interrupt (ESC/cancel).
        # Without this, session.cancelled stays True and _agent_loop breaks on
        # iteration 0, returning the user's own message as partial_result —
        # which then gets echoed back as the agent's "answer" (the follow-up
        # appears twice and the agent never actually runs).
        self.session.cancelled = False
        if not getattr(self, "_system_prompt", None):
            # No conversation yet — behave like a fresh run.
            return await self.run(task)
        self.session.current_agent = self.name
        self.session.transition_status("running", "continued_turn_started")
        self.session.start_turn(task, self.name)
        self._append_message(Message(role="user", content=task), source="continue_run")
        return await self._agent_loop()

    async def _agent_loop(self) -> dict:
        system_prompt = self._system_prompt
        # Raised from 50: the real governor is now the progress-aware stop below,
        # which bails when the agent thrashes and lets it keep going when it's
        # still gaining signal. Configurable via settings.agent_max_iterations.
        max_iterations = getattr(settings, "agent_max_iterations", 120)
        stale_limit = getattr(settings, "agent_stale_turn_limit", 8)
        iteration = 0
        # Natural prose returned immediately before the continuation guard.
        # Some providers obey the guard by sending finish_task in a separate
        # response; retain the prose so a no-op greeting does not acquire a
        # second, robotic lifecycle summary.
        guarded_text_completion: Optional[str] = None

        while iteration < max_iterations:
            if self.session.cancelled:
                break
            # Block here while the session is paused (no-op when running).
            await self.session.wait_if_paused()
            if self.session.cancelled:
                break
            iteration += 1
            self.session.record_event(
                "agent_iteration_started",
                {
                    "iteration": iteration,
                    "max_iterations": max_iterations,
                    "stale_turns": self._stale_turns,
                    "message_count": len(self.messages),
                    "estimated_input_tokens": self._estimate_tokens(
                        self.messages, system_prompt
                    ),
                    "context_limit": self.context_manager.context_window_limit,
                },
                agent=self.name,
            )

            self._inject_pending_instructions()

            self._preflight_compact(system_prompt)

            # Pin the durable action-ledger into the system prompt every turn so
            # the agent always sees what it already tried, even after compaction.
            effective_system = system_prompt + self._ledger_block()

            try:
                response = await self.llm.chat(
                    messages=self.messages,
                    tools=self.get_tools(),
                    system=effective_system,
                    on_chunk=self._stream_chunk,
                )
            except openai.BadRequestError as e:
                err_msg = str(e)
                if "too long" in err_msg or "too many tokens" in err_msg.lower() or "context length" in err_msg:
                    logger.warning(f"[{self.name}] Context overflow on LLM call — compacting and retrying")
                    self.session.record_event(
                        "context_overflow",
                        {"iteration": iteration, "error": err_msg},
                        agent=self.name,
                    )
                    self.messages = self.context_manager.compact_messages(self.messages, keep_recent_turns=2)
                    estimated = self._estimate_tokens(self.messages, system_prompt)
                    if estimated > self.context_manager.context_window_limit * 0.85:
                        self.messages = self.context_manager.emergency_compact(self.messages)

                    try:
                        response = await self.llm.chat(
                            messages=self.messages,
                            tools=self.get_tools(),
                            system=effective_system,
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

            self._record_response_usage(response)

            has_finish_task = any(
                tool_call.name == "finish_task"
                for tool_call in response.tool_calls
            )
            if response.content and not has_finish_task:
                self.session.add_trace(
                    agent=self.name,
                    event_type="thinking",
                    content=response.content,
                    model_call_id=response.model_call_id,
                    metadata={
                        "finish_reason": response.finish_reason,
                        "response_id": response.response_id,
                        "returned_model": response.returned_model,
                    },
                )

            if not response.tool_calls:
                assistant_msg = Message(role="assistant", content=response.content)
                self._append_message(assistant_msg, source="model_text_response")
                if self.requires_finish_task:
                    guarded_text_completion = (response.content or "").strip() or None
                    finish_reason = (response.finish_reason or "unknown").lower()
                    if finish_reason in {"content_filter", "safety"}:
                        result = {
                            "error": "Model response was blocked by the provider",
                            "finish_reason": response.finish_reason,
                            "partial_result": response.content or "",
                        }
                        self.session.record_event(
                            "finish_reason_handled",
                            {"action": "stop_error", **result},
                            agent=self.name,
                            model_call_id=response.model_call_id,
                        )
                        self.session.finish_turn("failed", result, agent=self.name)
                        return result
                    guard_reason = (
                        "output_limit"
                        if finish_reason in {"length", "max_tokens"}
                        else "missing_finish_task"
                    )
                    guard = (
                        "[CONTINUATION GUARD]: You have not called finish_task, so "
                        "the task is not complete. Continue executing any action you "
                        "promised. If all requested work is genuinely complete, call "
                        "finish_task with the complete operator-facing answer. If your "
                        "preceding response was already the complete answer, copy it "
                        "verbatim into summary; do not replace it with a recap."
                    )
                    self._append_message(
                        Message(role="user", content=guard),
                        source="continuation_guard",
                    )
                    self.session.record_event(
                        "continuation_guard_triggered",
                        {
                            "reason": guard_reason,
                            "finish_reason": response.finish_reason,
                            "iteration": iteration,
                            "assistant_content": response.content,
                        },
                        agent=self.name,
                        model_call_id=response.model_call_id,
                    )
                    continue
                result = self._parse_final_response(response.content or "")
                self.session.record_event(
                    "finish_reason_handled",
                    {
                        "finish_reason": response.finish_reason,
                        "action": "accept_text_completion",
                    },
                    agent=self.name,
                    model_call_id=response.model_call_id,
                )
                self.session.finish_turn("completed", result, agent=self.name)
                return result

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
            )
            self._append_message(assistant_msg, source="model_tool_response")

            made_new_call = False
            gained_signal = False
            for tool_call in response.tool_calls:
                is_finish_task = tool_call.name == "finish_task"
                if not is_finish_task:
                    self.session.add_trace(
                        agent=self.name,
                        event_type="tool_call",
                        content=f"Calling {tool_call.name}",
                        tool_name=tool_call.name,
                        tool_input=tool_call.arguments,
                        model_call_id=response.model_call_id,
                        tool_call_id=tool_call.id,
                        metadata={
                            "raw_arguments": tool_call.raw_arguments,
                            "parse_error": tool_call.parse_error,
                        },
                    )

                key = self._call_key(tool_call.name, tool_call.arguments)
                if key in self._call_cache:
                    # Anti-repetition: this exact call already ran — don't
                    # re-execute (saves tokens) and nudge the agent to move on.
                    prev_turn, prev_summ = self._call_cache[key]
                    result = {
                        "repeated_call": True,
                        "note": (
                            f"You already ran this exact call on turn {prev_turn}; the "
                            f"result was: {prev_summ}. Re-running it won't help — try a "
                            f"different payload, endpoint, or technique."
                        ),
                    }
                else:
                    made_new_call = True
                    result = None
                    # A tool raising must NOT kill the whole turn — turn it into an
                    # error result the model sees and can recover from (the most
                    # common trigger is malformed/truncated tool-call arguments,
                    # e.g. a huge `command` that overran the token limit, so a
                    # required arg arrives missing). CancelledError is a
                    # BaseException, so `except Exception` lets ESC-interrupt through.
                    try:
                        tool_started = time.monotonic()
                        self.session.record_event(
                            "tool_execution_started",
                            {
                                "name": tool_call.name,
                                "arguments": tool_call.arguments,
                                "raw_arguments": tool_call.raw_arguments,
                                "parse_error": tool_call.parse_error,
                            },
                            agent=self.name,
                            model_call_id=response.model_call_id,
                            tool_call_id=tool_call.id,
                        )
                        if self.tools.is_async_tool(tool_call.name):
                            result = await self.tools.execute_tool_async(tool_call.name, tool_call.arguments)
                        else:
                            # Run sync tools in a worker thread so a long-running tool
                            # (nmap, nuclei, a slow shell command) doesn't block the
                            # event loop — keeps the TUI responsive and spinner animating.
                            import asyncio as _asyncio
                            result = await _asyncio.to_thread(
                                self.tools.execute_tool, tool_call.name, tool_call.arguments
                            )
                    except Exception as exc:
                        logger.warning(f"[{self.name}] Tool {tool_call.name} raised: {exc}", exc_info=True)
                        note = (
                            "The tool call errored — often malformed or truncated "
                            "arguments (e.g. a value so large it exceeded the token "
                            "limit and cut off a required field). Retry with valid, "
                            "smaller arguments, or a different approach."
                        )
                        # The classic version of this: writing a file with a
                        # `cat > f << 'EOF'` heredoc. The whole file body has to
                        # fit inside the tool-call JSON, so anything sizeable
                        # truncates and lands here — and the model tends to
                        # retry the identical doomed command. Name the fix.
                        cmd = str((tool_call.arguments or {}).get("command", ""))
                        if tool_call.name == "run_command" and (
                            not tool_call.arguments or "<<" in cmd or ">" in cmd
                        ):
                            note = (
                                "This looks like an attempt to write a file through the "
                                "shell. Do NOT retry the heredoc — the file body has to "
                                "fit inside the tool-call arguments, so it will truncate "
                                "again. Use the `write_file` tool instead, and for a long "
                                "file write it in chunks with append=true."
                            )
                        result = {"error": f"{tool_call.name} failed: {exc}", "note": note}
                    finally:
                        self.session.record_event(
                            "tool_execution_finished",
                            {
                                "name": tool_call.name,
                                "duration_seconds": time.monotonic() - tool_started,
                                "result": result,
                            },
                            agent=self.name,
                            model_call_id=response.model_call_id,
                            tool_call_id=tool_call.id,
                        )
                    # Record this genuine attempt into the durable ledger + cache
                    # (survives compaction, so the agent never forgets it tried it).
                    summary = self._result_summary(result)
                    self._call_cache[key] = (iteration, summary)
                    self._ledger.append(
                        f"t{iteration} · {tool_call.name} {self._arg_hint(tool_call.arguments)} → {summary}"
                    )
                    # Novelty = a result summary we haven't seen before. Near-dup
                    # thrash (same endpoint, tweaked payload, same "not injectable"
                    # response) produces no new summary, so it never counts as signal.
                    novelty_key = f"{tool_call.name}::{summary}"
                    if novelty_key not in self._seen_summaries:
                        gained_signal = True
                        self._seen_summaries.add(novelty_key)

                if not is_finish_task:
                    self.session.add_trace(
                        agent=self.name,
                        event_type="tool_result",
                        content=f"Result from {tool_call.name}",
                        tool_name=tool_call.name,
                        tool_output=result,
                        model_call_id=response.model_call_id,
                        tool_call_id=tool_call.id,
                    )

                raw_content = json.dumps(result) if isinstance(result, dict) else str(result)
                truncated_content = self.context_manager.truncate_tool_result(tool_call.name, raw_content)
                tool_result = ToolResult(
                    tool_call_id=tool_call.id,
                    content=truncated_content,
                )
                self._append_message(
                    tool_result.to_message(), source=f"tool_result:{tool_call.name}"
                )

                if tool_call.name == "finish_task" and result.get("finished"):
                    operator_answer = self._operator_answer_for_completion(
                        summary=result["summary"],
                        reason=result["reason"],
                        response_content=response.content,
                        guarded_content=guarded_text_completion,
                    )
                    final = self._parse_final_response(operator_answer)
                    final["finish_reason"] = result["reason"]
                    if result.get("verification"):
                        final["verification"] = result["verification"]
                    self.session.record_event(
                        "finish_task_accepted",
                        {**result, "operator_answer": operator_answer},
                        agent=self.name,
                        model_call_id=response.model_call_id,
                        tool_call_id=tool_call.id,
                    )
                    self.session.finish_turn("completed", final, agent=self.name)
                    return final

            # Progress-aware stop: a turn is "productive" only if it ran a genuinely
            # new call AND that call produced a novel result (new signal). Turns that
            # only repeat calls — or run new calls that yield already-seen results
            # (near-dup thrash: same endpoint, tweaked payload, same response) — are
            # stale. Bail after enough consecutive stale turns rather than grinding
            # to the iteration cap re-deriving dead ends.
            if made_new_call and gained_signal:
                self._stale_turns = 0
            else:
                self._stale_turns += 1
                if self._stale_turns >= stale_limit:
                    reason = "no new action" if not made_new_call else "no new signal"
                    logger.info(f"[{self.name}] Stopping: {self._stale_turns} turns with {reason} (thrash)")
                    result = {
                        "error": "No further progress",
                        "partial_result": self._last_assistant_content(),
                    }
                    self.session.record_event(
                        "agent_loop_stopped",
                        {"reason": "stale_limit", "detail": reason, **result},
                        agent=self.name,
                    )
                    self.session.finish_turn("stale", result, agent=self.name)
                    return result

            if self.context_manager.needs_compaction():
                before = [m.to_dict() for m in self.messages]
                self.messages = self.context_manager.compact_messages(self.messages)
                self.session.record_event(
                    "context_compacted",
                    {
                        "mode": "usage_threshold",
                        "input_tokens": self.context_manager.last_input_tokens,
                        "messages_before": before,
                        "messages_after": [m.to_dict() for m in self.messages],
                    },
                    agent=self.name,
                )
                logger.info(f"[{self.name}] Compacted message history ({self.context_manager.last_input_tokens} input tokens)")

        reason = "cancelled" if self.session.cancelled else "max_iterations"
        result = {
            "error": "Cancelled" if self.session.cancelled else "Max iterations reached",
            "partial_result": self._last_assistant_content(),
        }
        self.session.record_event(
            "agent_loop_stopped",
            {"reason": reason, **result},
            agent=self.name,
        )
        self.session.finish_turn(reason, result, agent=self.name)
        return result

    def _parse_final_response(self, content: str) -> dict:
        return {"response": content}
