import asyncio

from openhack.agents.base import BaseAgent
from openhack.agents.interactive import InteractiveAgent
from openhack.agents.llm import LLMResponse, ToolCall
from openhack.agents.session import Session
from openhack.tools.registry import ToolRegistry


class _ScriptedLLM:
    model = "test-model"
    provider = "test-provider"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.event_callback = None

    async def chat(self, **kwargs):
        response = self.responses[self.calls]
        self.calls += 1
        return response


class _TextAgent(BaseAgent):
    name = "text-agent"

    def get_system_prompt(self, context):
        return "Answer."


def _session(tmp_path):
    return Session(str(tmp_path), persist_events=False)


def test_text_only_completion_is_kept_in_message_history(tmp_path):
    llm = _ScriptedLLM([
        LLMResponse(content="done", finish_reason="stop", model_call_id="m1")
    ])
    session = _session(tmp_path)
    agent = _TextAgent(llm, ToolRegistry(tmp_path), session)
    result = asyncio.run(agent.run("go"))

    assert result == {"response": "done"}
    assert agent.messages[-1].role == "assistant"
    assert agent.messages[-1].content == "done"
    assert any(e.event_type == "turn_finished" for e in session.events)


def test_interactive_agent_continues_until_finish_task(tmp_path):
    llm = _ScriptedLLM([
        LLMResponse(
            content="I understand the exploit. Let me write it.",
            finish_reason="stop",
            model_call_id="m1",
        ),
        LLMResponse(
            tool_calls=[
                ToolCall(
                    id="finish-1",
                    name="finish_task",
                    arguments={
                        "summary": "Exploit written and verified.",
                        "reason": "completed",
                        "verification": "smoke test passed",
                    },
                )
            ],
            finish_reason="tool_calls",
            model_call_id="m2",
        ),
    ])
    session = _session(tmp_path)
    tools = ToolRegistry(tmp_path, include_agent_tools=True, session=session)
    agent = InteractiveAgent(llm, tools, session)

    result = asyncio.run(agent.run("write the exploit", {"target_dir": str(tmp_path)}))

    assert llm.calls == 2
    assert result["response"] == "Exploit written and verified."
    assert result["finish_reason"] == "completed"
    assert any(
        e.event_type == "continuation_guard_triggered"
        and e.data["reason"] == "missing_finish_task"
        for e in session.events
    )
    assert any(e.event_type == "finish_task_accepted" for e in session.events)


def test_length_finish_reason_forces_continuation(tmp_path):
    llm = _ScriptedLLM([
        LLMResponse(content="partial", finish_reason="length", model_call_id="m1"),
        LLMResponse(
            tool_calls=[
                ToolCall(
                    id="finish-2",
                    name="finish_task",
                    arguments={"summary": "complete", "reason": "completed"},
                )
            ],
            finish_reason="tool_calls",
            model_call_id="m2",
        ),
    ])
    session = _session(tmp_path)
    agent = InteractiveAgent(
        llm,
        ToolRegistry(tmp_path, include_agent_tools=True, session=session),
        session,
    )
    result = asyncio.run(agent.run("go", {"target_dir": str(tmp_path)}))
    assert result["response"] == "complete"
    guard = next(e for e in session.events if e.event_type == "continuation_guard_triggered")
    assert guard.data["reason"] == "output_limit"


def test_no_action_completion_prefers_natural_text_and_hides_lifecycle_tool(tmp_path):
    llm = _ScriptedLLM([
        LLMResponse(
            content="Hi! What would you like to work on?",
            tool_calls=[
                ToolCall(
                    id="finish-greeting",
                    name="finish_task",
                    arguments={
                        "summary": (
                            "No task was requested — the session just started "
                            "with a greeting."
                        ),
                        "reason": "no_action_needed",
                        "verification": "No actions were pending.",
                    },
                )
            ],
            finish_reason="tool_calls",
            model_call_id="m1",
        )
    ])
    session = _session(tmp_path)
    agent = InteractiveAgent(
        llm,
        ToolRegistry(tmp_path, include_agent_tools=True, session=session),
        session,
    )

    result = asyncio.run(agent.run("hi", {"target_dir": str(tmp_path)}))

    assert result["response"] == "Hi! What would you like to work on?"
    assert not any(
        e.event_type in {"thinking", "tool_call", "tool_result"}
        for e in session.trace
    )
    accepted = next(e for e in session.events if e.event_type == "finish_task_accepted")
    assert accepted.data["operator_answer"] == result["response"]


def test_no_action_completion_reuses_text_from_guarded_previous_response(tmp_path):
    llm = _ScriptedLLM([
        LLMResponse(
            content="Hey! What would you like to work on?",
            finish_reason="stop",
            model_call_id="m1",
        ),
        LLMResponse(
            tool_calls=[
                ToolCall(
                    id="finish-greeting-2",
                    name="finish_task",
                    arguments={
                        "summary": (
                            "No task was requested — the operator just said hello. "
                            "Awaiting a concrete task to work on."
                        ),
                        "reason": "no_action_needed",
                        "verification": "No actions were taken.",
                    },
                )
            ],
            finish_reason="tool_calls",
            model_call_id="m2",
        ),
    ])
    session = _session(tmp_path)
    agent = InteractiveAgent(
        llm,
        ToolRegistry(tmp_path, include_agent_tools=True, session=session),
        session,
    )

    result = asyncio.run(agent.run("hello", {"target_dir": str(tmp_path)}))

    natural = "Hey! What would you like to work on?"
    assert result["response"] == natural
    visible = [
        e for e in session.trace
        if e.event_type in {"thinking", "tool_call", "tool_result"}
    ]
    assert [(e.event_type, e.content) for e in visible] == [("thinking", natural)]
    accepted = next(e for e in session.events if e.event_type == "finish_task_accepted")
    assert accepted.data["summary"].startswith("No task was requested")
    assert accepted.data["operator_answer"] == natural


def test_completed_answer_wins_over_shorter_finish_task_recap(tmp_path):
    poem = (
        "Sure — here's a short one:\n\n"
        "Terminal Light\n\n"
        "A cursor blinks in quiet night,\n"
        "where lines of code take shape and flight.\n\n"
        "Want a haiku or sonnet instead?"
    )
    llm = _ScriptedLLM([
        LLMResponse(content=poem, finish_reason="stop", model_call_id="m1"),
        LLMResponse(
            tool_calls=[
                ToolCall(
                    id="finish-poem",
                    name="finish_task",
                    arguments={
                        "summary": "Wrote a short poem and offered other styles.",
                        "reason": "completed",
                    },
                )
            ],
            finish_reason="tool_calls",
            model_call_id="m2",
        ),
    ])
    session = _session(tmp_path)
    agent = InteractiveAgent(
        llm,
        ToolRegistry(tmp_path, include_agent_tools=True, session=session),
        session,
    )

    result = asyncio.run(agent.run("write a poem", {"target_dir": str(tmp_path)}))

    assert result["response"] == poem
    visible = [e.content for e in session.trace if e.event_type == "thinking"]
    assert visible == [poem]


def test_unfinished_promise_does_not_override_completion_summary():
    answer = BaseAgent._operator_answer_for_completion(
        summary="Exploit written and verified.",
        reason="completed",
        response_content=None,
        guarded_content="I understand the exploit chain. Let me write it now.",
    )

    assert answer == "Exploit written and verified."
