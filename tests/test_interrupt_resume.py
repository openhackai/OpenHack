"""Regression: ESC-interrupting a run must not wedge the session.

An interrupt sets ``session.cancelled = True`` so the agent loop breaks out of
the current turn. If that flag is never cleared, the *next* user turn
(``continue_run``) breaks on iteration 0 and returns the user's own message as
``partial_result`` — which the TUI then echoes back as the agent's "answer"
(the follow-up appears twice and the agent never actually runs). ``continue_run``
must clear the flag so a follow-up genuinely resumes the conversation.
"""

import asyncio
from types import SimpleNamespace

from openhack.agents.base import BaseAgent
from openhack.agents.llm import LLMResponse, Message
from openhack.agents.session import Session
from openhack.tui import OpenHackApp, ScanState


class _FakeLLM:
    """Minimal LLMClient stand-in: one text response, no tool calls."""

    model = "grok-4.5"

    def __init__(self, reply: str = "real answer"):
        self.reply = reply
        self.calls = 0

    async def chat(self, *args, **kwargs):
        self.calls += 1
        return LLMResponse(
            content=self.reply,
            tool_calls=[],
            usage={"total_tokens": 5, "input_tokens": 3, "output_tokens": 2},
            cost=0.0,
        )


class _TinyAgent(BaseAgent):
    name = "tiny"

    def get_system_prompt(self, context: dict) -> str:
        return "you are a test agent"

    def get_tools(self) -> list[dict]:
        return []


def _make_agent(reply: str = "real answer"):
    session = Session(target_dir="/tmp")
    llm = _FakeLLM(reply)
    agent = _TinyAgent(llm, tools=None, session=session)
    # Simulate a conversation that already ran one turn, then got interrupted.
    agent._system_prompt = agent.get_system_prompt({})
    agent.messages = [Message(role="user", content="original task")]
    return agent, session, llm


def test_continue_run_clears_prior_interrupt():
    agent, session, llm = _make_agent(reply="real answer")
    # An interrupt happened: the loop was told to bail and the flag is still set.
    session.cancelled = True

    result = asyncio.run(agent.continue_run("hello again"))

    # The flag is cleared and the loop actually ran (llm was hit)...
    assert session.cancelled is False
    assert llm.calls == 1
    # ...returning the model's answer, NOT the user's own message echoed back.
    assert result == {"response": "real answer"}
    assert result.get("partial_result") is None


def test_follow_up_not_echoed_as_agent_answer():
    agent, session, llm = _make_agent(reply="real answer")
    session.cancelled = True

    asyncio.run(agent.continue_run("hello again"))

    # No trace should attribute the user's follow-up text to the agent — that was
    # the double-echo bug (partial_result == the just-appended user message).
    echoed = [
        e for e in session.trace
        if e.agent == agent.name and (e.content or "").strip() == "hello again"
    ]
    assert echoed == []


def test_continue_run_clears_flag_even_on_fresh_fallback():
    # No prior conversation → continue_run falls back to run(); the flag must
    # still be cleared so the fresh run isn't dead on arrival.
    session = Session(target_dir="/tmp")
    llm = _FakeLLM("fresh answer")
    agent = _TinyAgent(llm, tools=None, session=session)
    session.cancelled = True

    result = asyncio.run(agent.continue_run("first task"))

    assert session.cancelled is False
    assert llm.calls == 1
    assert result == {"response": "fresh answer"}


def test_interrupted_live_response_is_committed_to_transcript():
    app = OpenHackApp.__new__(OpenHackApp)
    app._stream_buf = "An answer that was still streaming"
    app.scan = ScanState(target="/tmp")
    session = Session(target_dir="/tmp", on_trace=app._on_trace, persist_events=False)
    agent = SimpleNamespace(name="openhack")

    app._commit_interrupted_stream(session, agent)

    assert session.trace[-1].content == "An answer that was still streaming"
    assert session.trace[-1].metadata == {"partial": True, "interrupted": True}
    assert app._stream_buf == ""


def test_queued_messages_are_shown_until_agent_consumes_them():
    app = OpenHackApp.__new__(OpenHackApp)
    app.session = Session(target_dir="/tmp", persist_events=False)
    app.agent = SimpleNamespace(_instructions_watermark=0)
    app.scan_task = SimpleNamespace(done=lambda: False)
    app.session.add_user_instruction("make the answer shorter")
    app.session.add_user_instruction("also include the proof")

    rendered = "".join(text for _, text in app._queued_message_fragments())

    assert "queued 2" in rendered
    assert "make the answer shorter" in rendered
    assert "also include the proof" in rendered

    app.agent._instructions_watermark = 2
    assert app._queued_message_fragments() == []
