"""Opt-in spend / wall-clock backstops for the agent loop.

With the iteration cutoff removed, a run that keeps producing novel output has
no bound other than the stale-turn stop. `agent_max_cost_usd` and
`agent_max_runtime_s` give operators a hard ceiling. Both default to 0
(unlimited) so existing behaviour is unchanged.
"""

import asyncio

from openhack.agents.llm import LLMResponse, ToolCall
from openhack.agents.session import Session
from openhack.config import settings
from openhack.tui import OpenHackApp

from tests.test_no_iteration_cutoff import _ProbeAgent, _UniqueResultTools


class _CostlyLLM:
    """Productive forever (never finish_task); every call costs `cost_per_call`."""

    model = "test-model"
    provider = "test-provider"

    def __init__(self, cost_per_call: float = 0.0, delay: float = 0.0):
        self.cost_per_call = cost_per_call
        self.delay = delay
        self.calls = 0
        self.event_callback = None

    async def chat(self, **kwargs):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return LLMResponse(
            tool_calls=[
                ToolCall(id=f"p{self.calls}", name="probe", arguments={"n": self.calls})
            ],
            finish_reason="tool_calls",
            model_call_id=f"m{self.calls}",
            cost=self.cost_per_call,
        )


def _run(tmp_path, llm, monkeypatch, max_calls=50):
    session = Session(str(tmp_path), persist_events=False)
    tools = _UniqueResultTools(tmp_path, include_agent_tools=True, session=session)
    agent = _ProbeAgent(llm, tools, session)
    # Safety net so a regression in the cap can't hang the suite.
    monkeypatch.setattr(settings, "agent_max_iterations", max_calls)
    return session, asyncio.run(agent.run("go"))


def test_defaults_are_unlimited():
    assert settings.agent_max_cost_usd == 0.0
    assert settings.agent_max_runtime_s == 0.0


def test_cost_cap_stops_the_loop(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "agent_max_cost_usd", 0.25)
    llm = _CostlyLLM(cost_per_call=0.10)

    session, result = _run(tmp_path, llm, monkeypatch)

    # 3 calls -> $0.30 >= $0.25, checked before the 4th call.
    assert llm.calls == 3
    assert result["error"] == "Budget exceeded"
    assert "agent_max_cost_usd" in result["detail"]
    assert any(
        e.event_type == "agent_loop_stopped"
        and (e.data or {}).get("reason") == "budget_exceeded"
        for e in session.events
    )


def test_runtime_cap_stops_the_loop(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "agent_max_cost_usd", 0.0)
    monkeypatch.setattr(settings, "agent_max_runtime_s", 0.2)
    llm = _CostlyLLM(delay=0.08)

    session, result = _run(tmp_path, llm, monkeypatch)

    assert result["error"] == "Budget exceeded"
    assert "agent_max_runtime_s" in result["detail"]
    assert 2 <= llm.calls <= 6


def test_caps_disabled_do_not_interfere(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "agent_max_cost_usd", 0.0)
    monkeypatch.setattr(settings, "agent_max_runtime_s", 0.0)
    llm = _CostlyLLM(cost_per_call=100.0)

    _, result = _run(tmp_path, llm, monkeypatch, max_calls=7)

    # Only the iteration backstop (7) stops it, not the budget.
    assert llm.calls == 7
    assert result["error"] == "Max iterations reached"


def test_budget_stop_is_reported_incomplete_not_completed():
    result = {"error": "Budget exceeded", "detail": "x", "partial_result": ""}
    assert OpenHackApp._agent_result_status(result) == "incomplete"
