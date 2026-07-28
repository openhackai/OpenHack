"""Scope discipline: the agent must not invent work from a non-request.

A stray "et" in ~/Documents/openhack/app kicked off a four-tool recon sweep
(get_project_info, get_route_map, get_middleware_config, get_server_actions)
before the operator could stop it. Ambiguity is not authorization.
"""

from openhack.agents.interactive import PLAN_SYSTEM_PROMPT, SYSTEM_PROMPT


def test_non_request_input_must_not_trigger_tools():
    assert "zero tools" in SYSTEM_PROMPT
    assert "not** a task brief" in SYSTEM_PROMPT
    # The specific failure shape: a small input inflated into a big engagement.
    assert "Never infer a large engagement from a small input" in SYSTEM_PROMPT


def test_ambiguous_input_asks_rather_than_guessing():
    assert "ask which" in SYSTEM_PROMPT
    assert "state the goal back" in SYSTEM_PROMPT


def test_greetings_are_not_recon():
    assert "Greetings" in SYSTEM_PROMPT


def test_tight_loops_rule_no_longer_licenses_acting_on_a_guess():
    # "Work in tight loops: act…" used to read as a standing instruction to act.
    assert "don't start the loop on a guess" in SYSTEM_PROMPT
    assert "Once the task is clear" in SYSTEM_PROMPT


def test_plan_mode_also_refuses_to_sweep_on_an_unclear_objective():
    assert "gather nothing" in PLAN_SYSTEM_PROMPT
