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
    reasoning_details = [{
        "type": "reasoning.text",
        "text": "Need to write the exploit.",
        "id": "reasoning-1",
        "format": "openrouter-v1",
        "index": 0,
    }]
    llm = _ScriptedLLM([
        LLMResponse(
            content="I understand the exploit. Let me write it.",
            reasoning_details=reasoning_details,
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
    assert agent.messages[1].reasoning_details == reasoning_details
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
    visible = [
        e for e in session.trace
        if e.event_type in {"thinking", "tool_call", "tool_result"}
    ]
    assert [(e.event_type, e.content) for e in visible] == [
        ("thinking", "Hi! What would you like to work on?")
    ]
    accepted = next(e for e in session.events if e.event_type == "finish_task_accepted")
    assert accepted.data["operator_answer"] == result["response"]


def test_natural_no_action_completion_needs_no_guard_call(tmp_path):
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
    assert llm.calls == 1
    handled = next(e for e in session.events if e.event_type == "finish_reason_handled")
    assert handled.data["action"] == "accept_natural_completion"
    assert not any(
        e.event_type in {"continuation_guard_triggered", "finish_task_accepted"}
        for e in session.events
    )


def test_complete_greeting_does_not_start_completion_followup(tmp_path):
    """Regression for lifecycle calls after greetings, including 7d2707d9."""

    class _StreamingScriptedLLM(_ScriptedLLM):
        async def chat(self, **kwargs):
            response = self.responses[self.calls]
            self.calls += 1
            on_chunk = kwargs.get("on_chunk")
            if on_chunk and response.content:
                on_chunk("content", response.content)
            return response

    natural = (
        "Hey — what are we working on? Point me at a target (code, URL, host) "
        "or a task and I'll get started."
    )
    retry = "No task yet — just a greeting, so nothing to run."
    lifecycle_recap = "\n\n"
    llm = _StreamingScriptedLLM([
        LLMResponse(content=natural, finish_reason="stop", model_call_id="m1"),
        LLMResponse(content=retry, finish_reason="stop", model_call_id="m2"),
        LLMResponse(
            content=lifecycle_recap,
            tool_calls=[
                ToolCall(
                    id="finish-greeting-3",
                    name="finish_task",
                    arguments={
                        "summary": (
                            "No task yet — just a greeting. Ready when you are: "
                            "give me a target and a goal and I'll get to work."
                        ),
                        "reason": "no_action_needed",
                    },
                )
            ],
            finish_reason="tool_calls",
            model_call_id="m3",
        ),
    ])
    session = _session(tmp_path)
    agent = InteractiveAgent(
        llm,
        ToolRegistry(tmp_path, include_agent_tools=True, session=session),
        session,
    )
    streamed = []
    agent.stream_callback = lambda kind, text: streamed.append((kind, text))

    result = asyncio.run(agent.run("hello", {"target_dir": str(tmp_path)}))

    assert llm.calls == 1
    assert result["response"] == natural
    assert streamed == [("content", natural)]
    visible = [e.content for e in session.trace if e.event_type == "thinking"]
    assert visible == [natural]
    assert not any(
        e.event_type in {"continuation_guard_triggered", "tool_execution_started"}
        for e in session.events
    )


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


def test_operator_dependent_promises_are_complete_handoffs():
    handoffs = [
        "Give me a target and I'll get started.",
        "Point me at the repo, then I will take a look.",
        "Please send the URL. I'm going to start once I have it.",
        "Once you provide the scope, I'll begin.",
        "I'll start after you choose one.",
    ]

    assert all(
        not BaseAgent._looks_like_unfinished_promise(handoff)
        for handoff in handoffs
    )


def test_autonomous_promises_still_trigger_continuation():
    promises = [
        "I found the endpoint. I'll scan it now.",
        "Let me write the exploit now.",
        "I'm going to run the tests next.",
        "After I inspect the route, I will report back.",
    ]

    assert all(
        BaseAgent._looks_like_unfinished_promise(promise)
        for promise in promises
    )


def test_let_me_know_invitation_keeps_full_completed_answer():
    essay = "# Hacking\n\n" + ("Complete essay paragraph. " * 20) + "Let me know."
    answer = BaseAgent._operator_answer_for_completion(
        summary="Wrote an essay on hacking.",
        reason="completed",
        response_content=essay,
        guarded_content=None,
    )

    assert answer == essay


def test_natural_completion_may_repeat_across_user_turns(tmp_path):
    llm = _ScriptedLLM([
        LLMResponse(content="Hi! What can I help you with?", finish_reason="stop"),
        LLMResponse(content="Hi! What can I help you with?", finish_reason="stop"),
    ])
    session = _session(tmp_path)
    agent = InteractiveAgent(
        llm,
        ToolRegistry(tmp_path, include_agent_tools=True, session=session),
        session,
    )

    first = asyncio.run(agent.run("hi", {"target_dir": str(tmp_path)}))
    second = asyncio.run(agent.continue_run("hi"))

    assert first["response"] == "Hi! What can I help you with?"
    assert second["response"] == "Hi! What can I help you with?"
    assert llm.calls == 2
    assert not [
        event for event in session.events
        if event.event_type == "finish_task_accepted"
    ]
