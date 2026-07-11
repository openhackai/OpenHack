"""Tests for the per-vuln-class specialist exploiter layer (additive, off by default)."""

from pathlib import Path

import pytest

from openhack.agents.session import Session
from openhack.agents.specialists import (
    build_specialist,
    classify_vuln_class,
    SPECIALIST_REGISTRY,
    XSSSpecialist,
    InjectionSpecialist,
    AuthSpecialist,
    BlindOOBSpecialist,
)
from openhack.tools.registry import ToolRegistry
from openhack.tools.stateful_browser import StatefulBrowserTools


@pytest.mark.parametrize("tag,expected", [
    ("xss", "xss"), ("reflected XSS", "xss"),
    ("sqli", "injection"), ("command_injection", "injection"), ("lfi", "injection"),
    ("blind_sqli", "blind"), ("out-of-band", "blind"),
    ("ssrf", "ssrf"), ("ssti", "ssti"), ("template injection", "ssti"),
    ("idor", "auth"), ("jwt", "auth"), ("default_credentials", "auth"),
    ("", "injection"),
])
def test_classify_vuln_class(tag, expected):
    assert classify_vuln_class(tag) == expected


def test_registry_has_all_specialists():
    assert set(SPECIALIST_REGISTRY) == {"xss", "injection", "ssrf", "ssti", "auth", "blind"}


def test_build_specialist_types(tmp_path):
    sess = Session(target_dir=str(tmp_path))
    assert isinstance(build_specialist("xss", str(tmp_path), sess), XSSSpecialist)
    assert isinstance(build_specialist("sqli", str(tmp_path), sess), InjectionSpecialist)
    assert isinstance(build_specialist("idor", str(tmp_path), sess), AuthSpecialist)
    assert isinstance(build_specialist("blind_sqli", str(tmp_path), sess), BlindOOBSpecialist)


def test_xss_specialist_has_stateful_browser(tmp_path):
    sess = Session(target_dir=str(tmp_path))
    xss = build_specialist("xss", str(tmp_path), sess)
    names = {t["name"] for t in xss.get_tools()}
    assert {"browser_navigate", "browser_fill", "browser_click"} <= names
    assert {"report_finding", "sqlmap_test", "oob_register"} <= names


def test_blind_specialist_no_stateful_browser(tmp_path):
    sess = Session(target_dir=str(tmp_path))
    blind = build_specialist("blind_sqli", str(tmp_path), sess)
    names = {t["name"] for t in blind.get_tools()}
    assert {"oob_register", "oob_poll", "sqlmap_test"} <= names
    assert "browser_navigate" not in names  # stateful browser only for XSS


def test_specialist_playbook_in_system_prompt(tmp_path):
    sess = Session(target_dir=str(tmp_path))
    xss = build_specialist("xss", str(tmp_path), sess)
    prompt = xss.get_system_prompt({"target_dir": str(tmp_path)})
    assert "XSS specialist" in prompt
    # still carries the base operator rules (reuse, not replace)
    assert "Swiss-army knife for hackers" in prompt


def test_stateful_browser_specs_exclude_report(tmp_path):
    sb = StatefulBrowserTools(evidence_dir=tmp_path)
    names = {t["name"] for t in sb.get_tool_definitions()}
    assert "browser_snapshot" in names and "browser_execute_js" in names
    assert "report_browser_result" not in names


def test_generalist_registry_unchanged(tmp_path):
    """The default agent-tools registry must NOT gain the stateful browser."""
    sess = Session(target_dir=str(tmp_path))
    reg = ToolRegistry(target_dir=tmp_path, include_agent_tools=True, session=sess)
    names = set(reg._async_handlers) | set(reg._tool_handlers)
    assert "browser_fetch" in names          # generalist keeps the one-shot browser
    assert "browser_navigate" not in names   # but NOT the stateful one


def test_dispatch_tool_only_on_interactive(tmp_path):
    """dispatch_specialist reaches the generalist, but not PlanAgent or specialists."""
    from openhack.agents.interactive import build_interactive_agent, build_plan_agent

    agent, _ = build_interactive_agent(str(tmp_path))
    assert "dispatch_specialist" in {t["name"] for t in agent.get_tools()}

    plan, _ = build_plan_agent(str(tmp_path))
    assert "dispatch_specialist" not in {t["name"] for t in plan.get_tools()}

    sess = Session(target_dir=str(tmp_path))
    xss = build_specialist("xss", str(tmp_path), sess)
    assert "dispatch_specialist" not in {t["name"] for t in xss.get_tools()}  # no recursion


def test_attach_source_is_additive(tmp_path):
    """attach_source adds a tool without dropping existing ones."""
    sess = Session(target_dir=str(tmp_path))
    reg = ToolRegistry(target_dir=tmp_path, include_agent_tools=True, session=sess)
    before = set(reg._async_handlers) | set(reg._tool_handlers)
    from openhack.tools.specialist_dispatch import SpecialistDispatchTools
    reg.attach_source(SpecialistDispatchTools(tmp_path, sess, model="grok-4.5"))
    after = set(reg._async_handlers) | set(reg._tool_handlers)
    assert before < after and "dispatch_specialist" in after
