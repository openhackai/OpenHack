"""Regression: the agent loop has no iteration cutoff.

Session 19fb4d79 stopped mid-work at exactly 60 turns — the model had just
written a file successfully and said it still had work left, but the loop hit
`agent_max_iterations` and terminated unconditionally. Worse, the TUI recorded
that run as `completed`, so a cut-off session looked like a clean finish.

Work now ends only via finish_task, cancel, a real error, or the progress-aware
stale-turn stop.
"""

import asyncio

import pytest

from openhack.agents.base import BaseAgent
from openhack.agents.llm import LLMResponse, ToolCall
from openhack.agents.session import Session
from openhack.config import settings
from openhack.tools.registry import ToolRegistry
from openhack.tui import OpenHackApp


class _ProductiveLLM:
    """Emits a fresh, never-repeated tool call each turn, then finish_task."""

    model = "test-model"
    provider = "test-provider"

    def __init__(self, productive_turns: int):
        self.productive_turns = productive_turns
        self.calls = 0
        self.event_callback = None

    async def chat(self, **kwargs):
        self.calls += 1
        if self.calls > self.productive_turns:
            return LLMResponse(
                tool_calls=[
                    ToolCall(
                        id=f"finish-{self.calls}",
                        name="finish_task",
                        arguments={
                            "summary": "Done.",
                            "reason": "completed",
                            "verification": "checked",
                        },
                    )
                ],
                finish_reason="tool_calls",
                model_call_id=f"m{self.calls}",
            )
        return LLMResponse(
            tool_calls=[
                ToolCall(
                    id=f"probe-{self.calls}",
                    name="probe",
                    arguments={"n": self.calls},
                )
            ],
            finish_reason="tool_calls",
            model_call_id=f"m{self.calls}",
        )


class _UniqueResultTools(ToolRegistry):
    """Every call returns a distinct summary, so no turn is ever 'stale'."""

    def is_async_tool(self, name: str) -> bool:
        return False if name == "probe" else super().is_async_tool(name)

    def execute_tool(self, name: str, arguments: dict):
        if name != "probe":
            return super().execute_tool(name, arguments)
        return {"status": f"probe {arguments.get('n')} returned new signal"}


class _ProbeAgent(BaseAgent):
    name = "probe-agent"

    def get_system_prompt(self, context):
        return "Probe."


def _run(tmp_path, productive_turns):
    llm = _ProductiveLLM(productive_turns)
    session = Session(str(tmp_path), persist_events=False)
    tools = _UniqueResultTools(tmp_path, include_agent_tools=True, session=session)
    agent = _ProbeAgent(llm, tools, session)
    return llm, session, asyncio.run(agent.run("go"))


def test_loop_runs_far_past_the_old_sixty_turn_cap(tmp_path):
    llm, session, result = _run(tmp_path, productive_turns=100)

    # 100 productive turns + the finish_task turn.
    assert llm.calls == 101
    assert "error" not in result
    assert not any(
        e.event_type == "agent_loop_stopped"
        and (e.content or {}).get("reason") == "max_iterations"
        for e in session.events
    )


def test_default_settings_impose_no_cap():
    assert settings.agent_max_iterations == 0


def test_positive_cap_is_still_honoured_when_opted_into(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "agent_max_iterations", 5)
    llm, session, result = _run(tmp_path, productive_turns=100)

    assert llm.calls == 5
    assert result["error"] == "Max iterations reached"


@pytest.mark.parametrize(
    "result,expected",
    [
        ({"response": "all done"}, "completed"),
        ({"error": "Max iterations reached", "partial_result": "x"}, "incomplete"),
        ({"error": "No further progress", "partial_result": "x"}, "incomplete"),
        ({"error": "Cancelled", "partial_result": "x"}, "cancelled"),
    ],
)
def test_unfinished_runs_are_never_reported_as_completed(result, expected):
    assert OpenHackApp._agent_result_status(result) == expected
