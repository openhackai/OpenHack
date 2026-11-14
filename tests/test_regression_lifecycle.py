"""Regression tests inside the existing vulnerability lifecycle.

discovery (hunters) → validation → sandbox exploit verification → Step 6
regression tests, then report persistence, the --regression-tests flag,
configuration, and the TUI's findings view and /copy remediation prompt.
The LLM-backed scan stages are stubbed with canned results; the coordinator,
evidence capture and the regression pipeline are real.
"""

import asyncio
import contextlib
import io
import json
import time
from types import SimpleNamespace

import pytest

import openhack.regression.pipeline as pipeline_mod
from openhack import config
from openhack.agents.coordinator import CoordinatorAgent
from openhack.agents.session import Finding, Session, TraceEntry
from openhack.config import settings
from openhack.regression import headless
from openhack.regression.pipeline import PipelineOptions
from openhack.regression.workspace import RevisionSpec
from openhack.tools.registry import ToolRegistry
from tests.regression_support import (
    FIX_BRANCH,
    TEST_PATH,
    FakeRunner,
    ScriptedLLM,
    git,
    install_scan_stubs,
    make_fixture_repo,
    submit,
    verified_idor_finding,
)


@pytest.fixture
def lifecycle(monkeypatch, tmp_path):
    """The coordinator with stubbed discovery/validation/sandbox stages."""
    for key, value in {
        "checkpoint_enabled": False, "feature_hunt_enabled": False, "sandbox_enabled": True,
        "browser_verification_enabled": False, "regression_tests_enabled": True,
        "regression_tests_fixed_ref": FIX_BRANCH,
    }.items():
        monkeypatch.setattr(settings, key, value)

    fx = make_fixture_repo(tmp_path)
    regression_llm = ScriptedLLM([submit()])
    runner = FakeRunner()
    llms = install_scan_stubs(monkeypatch, regression_llm)
    monkeypatch.setattr(pipeline_mod, "ContainerTestRunner", lambda limits=None: runner)
    monkeypatch.setattr(pipeline_mod, "default_artifacts_root", lambda session: tmp_path / "artifacts")

    def scan():
        session = Session(str(fx["repo"]), persist_events=False)
        agent = CoordinatorAgent(ScriptedLLM([]), ToolRegistry(target_dir=fx["repo"]), session)
        return session, asyncio.run(agent.run_full_scan())

    return SimpleNamespace(fx=fx, scan=scan, runner=runner, llm=regression_llm, llms=llms)


def test_verified_idor_produces_a_validated_regression_test_during_a_scan(lifecycle):
    session, result = lifecycle.scan()
    finding = session.findings[0]

    # Sandbox verification kept a redacted record of the exploit...
    assert finding.source == "sandbox_verified"
    request = finding.exploit_evidence["exploit_request"]
    assert (request["method"], request["path"]) == ("GET", "/api/notes/1")
    assert request["headers"]["Authorization"] == "[REDACTED]"
    assert "eyJhbGci" not in json.dumps(finding.exploit_evidence)
    # ...which Step 6 turned into a validated regression test.
    assert finding.regression_test["status"] == "validated"
    assert finding.regression_test["test_path"] == TEST_PATH
    assert result["findings"][0]["regressionTest"]["status"] == "validated"
    assert "regression_tests" in session.step_costs
    steps = [e.content for e in session.trace if e.event_type == "step_start"]
    assert any("Step 6: Security regression tests" in str(s) for s in steps)
    complete = [e.content for e in session.trace if e.event_type == "step_complete"
                and isinstance(e.content, dict) and e.content.get("step") == "regression_tests"]
    assert complete[0]["statuses"] == {"validated": 1}
    # The scanned working tree is the vulnerable revision; the fix comes from config.
    assert len(lifecycle.runner.runs_for("vulnerable")) == 2
    assert len(lifecycle.runner.runs_for("remediated")) == 2


def test_disabled_by_default_and_skipped_when_disabled(lifecycle, monkeypatch):
    assert config.Settings.model_fields["regression_tests_enabled"].default is False
    monkeypatch.setattr(settings, "regression_tests_enabled", False)
    monkeypatch.setattr(pipeline_mod, "RegressionTestPipeline",
                        lambda *a, **k: pytest.fail("pipeline must not run when disabled"))
    session, _ = lifecycle.scan()

    assert session.findings[0].regression_test is None
    assert "regression_tests" not in session.step_costs
    assert "regression_test" not in lifecycle.llms
    # Exploit evidence is still recorded so a later --regression-tests run can use it.
    assert session.findings[0].exploit_evidence is not None


def test_regression_step_failure_never_costs_the_scan_its_findings(lifecycle, monkeypatch):
    async def explode(self, findings):
        raise RuntimeError("docker exploded")

    monkeypatch.setattr(pipeline_mod.RegressionTestPipeline, "run", explode)
    session, result = lifecycle.scan()

    assert result["status"] == "completed"
    assert len(session.findings) == 1
    errors = [e.content for e in session.trace if e.event_type == "regression_test_error"]
    assert errors and "docker exploded" in errors[0]


def test_misconfigured_regression_model_does_not_fail_the_scan(lifecycle, monkeypatch):
    stubbed = CoordinatorAgent._create_llm_for_agent

    def create(self, agent_type):
        if agent_type == "regression_test":
            raise ValueError("unknown model 'nope'")
        return stubbed(self, agent_type)

    monkeypatch.setattr(CoordinatorAgent, "_create_llm_for_agent", create)
    session, result = lifecycle.scan()

    assert result["status"] == "completed" and len(session.findings) == 1
    errors = [e.content for e in session.trace if e.event_type == "regression_test_error"]
    assert errors and "unknown model 'nope'" in errors[0]
    assert session.step_costs["regression_tests"] == 0.0


def test_unverified_findings_report_missing_evidence(lifecycle, monkeypatch):
    monkeypatch.setattr(settings, "sandbox_enabled", False)
    session, _ = lifecycle.scan()
    status = session.findings[0].regression_test
    assert status["status"] == "missing_evidence"
    assert "enable sandbox verification" in status["reason"]
    assert lifecycle.llm.calls == 0


# ── reports ──

def test_finding_round_trips_through_a_report():
    original = verified_idor_finding(regression_test={"status": "validated", "test_path": TEST_PATH})
    data = json.loads(json.dumps(original.to_dict()))
    restored = Finding.from_dict(data)

    assert data["exploitEvidence"]["exploit_request"]["path"] == "/api/notes/1"
    assert restored.id == original.id
    assert restored.source == "sandbox_verified"
    assert restored.exploit_evidence == original.exploit_evidence
    assert restored.regression_test == {"status": "validated", "test_path": TEST_PATH}
    assert restored.fix == original.fix and restored.line_number == 70


def test_headless_report_records_the_scanned_revision(tmp_path, monkeypatch):
    import openhack.headless_scan as headless_scan

    fx = make_fixture_repo(tmp_path)
    monkeypatch.setattr(headless_scan, "SCANS_DIR", tmp_path / "scans")
    session = Session(str(fx["repo"]), persist_events=False)
    session.add_finding(verified_idor_finding(regression_test={"status": "validated"}))
    path = headless_scan._write_report(session, str(fx["repo"]), "completed", time.time())
    report = json.loads(path.read_text())

    assert report["repository"] == {"head": fx["vulnerable_sha"], "branch": "main", "dirty": False}
    assert report["findings"][0]["regressionTest"] == {"status": "validated"}


def test_headless_status_lines():
    from openhack.headless_scan import format_regression_status

    finding = verified_idor_finding(regression_test={
        "status": "validated", "status_label": "generated and validated", "test_path": TEST_PATH,
        "reason": "Fails on the vulnerable revision ...", "artifacts": {"patch": "/x/regression-test.patch"},
    })
    lines = format_regression_status(finding)
    assert lines[0] == f"Regression test: generated and validated — {TEST_PATH}"
    assert "Fails on the vulnerable revision" in lines[1]
    assert "git apply" in lines[2]
    assert format_regression_status(verified_idor_finding()) == []

    finding.regression_test["artifacts"]["repo_path"] = f"services/api/{TEST_PATH}"
    assert format_regression_status(finding)[0].endswith(f"— services/api/{TEST_PATH}")


# ── openhack --regression-tests ──

def _saved_scan(tmp_path, fx, finding, **repository):
    scans = tmp_path / "scans"
    scans.mkdir(exist_ok=True)
    report = {
        "scan_id": "a1b2c3d4-0000-4000-8000-000000000000",
        "target_dir": str(fx["repo"]),
        "status": "completed",
        "repository": {"head": fx["vulnerable_sha"], "branch": "main", "dirty": False, **repository},
        "findings": [finding.to_dict()],
    }
    path = scans / f"{report['scan_id']}.json"
    path.write_text(json.dumps(report))
    return path


@pytest.fixture
def cli_env(monkeypatch, tmp_path):
    runner = FakeRunner()
    llm = ScriptedLLM([submit()])
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr(headless, "SCANS_DIR", tmp_path / "scans")
    monkeypatch.setattr(headless, "reload_settings", lambda: None)
    monkeypatch.setattr("openhack.agents.llm.LLMClient", lambda *a, **k: llm)
    monkeypatch.setattr(pipeline_mod, "ContainerTestRunner", lambda limits=None: runner)
    monkeypatch.setattr(settings, "regression_tests_fixed_ref", None)
    return SimpleNamespace(runner=runner, llm=llm)


def test_regression_tests_flag_validates_against_the_fix_and_updates_the_report(tmp_path, cli_env):
    fx = make_fixture_repo(tmp_path)
    report_path = _saved_scan(tmp_path, fx, verified_idor_finding())
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = asyncio.run(headless.run_regression_tests_for_report("a1b2c3d4", FIX_BRANCH))

    assert code == 0
    report = json.loads(report_path.read_text())
    status = report["findings"][0]["regressionTest"]
    assert status["status"] == "validated"
    assert status["validation"]["vulnerable"]["resolved"] == fx["vulnerable_sha"]
    assert status["validation"]["remediated"]["resolved"] == fx["fixed_sha"]
    assert report["regression_tests"]["statuses"] == {"validated": 1}
    printed = out.getvalue()
    assert f"Vulnerable: git revision `{fx['vulnerable_sha']}`" in printed
    assert f"Remediated: git revision `{FIX_BRANCH}`" in printed
    assert "Regression test: generated and validated" in printed


def test_regression_tests_flag_reports_failures_with_a_nonzero_status(tmp_path, cli_env):
    fx = make_fixture_repo(tmp_path)
    _saved_scan(tmp_path, fx, verified_idor_finding())
    with contextlib.redirect_stdout(io.StringIO()):
        # No fix revision and HEAD has not moved: the test can only be pending.
        code = asyncio.run(headless.run_regression_tests_for_report("a1b2c3d4"))
    assert code == 2


def test_unknown_session(tmp_path, cli_env):
    (tmp_path / "scans").mkdir()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert asyncio.run(headless.run_regression_tests_for_report("nope")) == 1
    assert "not found" in out.getvalue()


def test_choose_revisions(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "regression_tests_fixed_ref", None)
    fx = make_fixture_repo(tmp_path)
    repo = fx["repo"]
    clean = {"repository": {"head": fx["vulnerable_sha"], "dirty": False}}

    vulnerable, remediation, _ = headless.choose_revisions(clean, repo, None)
    assert vulnerable == RevisionSpec.git(fx["vulnerable_sha"]) and remediation is None

    git(repo, "merge", "-q", "--ff-only", FIX_BRANCH)  # the fix landed on main
    _, remediation, notes = headless.choose_revisions(clean, repo, None)
    assert remediation == RevisionSpec.git(fx["fixed_sha"]) and "using HEAD" in notes[-1]

    vulnerable, _, notes = headless.choose_revisions({"repository": {"head": "x", "dirty": True}}, repo, None)
    assert vulnerable == RevisionSpec.worktree() and "uncommitted" in notes[0]

    _, remediation, _ = headless.choose_revisions(clean, repo, str(tmp_path))
    assert remediation.kind == "directory"


def test_cli_flag_usage_without_a_session(monkeypatch):
    import openhack.__main__ as main_mod

    monkeypatch.setattr("sys.argv", ["openhack", "--regression-tests"])
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        main_mod.main()
    assert "Usage: openhack --regression-tests <session_id> [fixed-revision]" in out.getvalue()


# ── configuration ──

def test_settings_can_be_enabled_from_the_environment(monkeypatch):
    monkeypatch.setenv("REGRESSION_TESTS_ENABLED", "true")
    monkeypatch.setenv("REGRESSION_TESTS_FIXED_REF", "fix/idor")
    fresh = config._build_settings()
    assert fresh.regression_tests_enabled is True
    assert fresh.regression_tests_fixed_ref == "fix/idor"


def test_pipeline_options_from_settings(tmp_path):
    stub = SimpleNamespace(
        regression_tests_fixed_ref="fix/idor", regression_tests_max_attempts=0,
        regression_tests_validation_runs=3, regression_tests_base_image="registry.local/python:3.12",
        regression_tests_timeout=45, regression_tests_build_timeout=300,
    )
    options = PipelineOptions.from_settings(stub, str(tmp_path), report_id="abc")
    assert options.remediation == RevisionSpec.git("fix/idor")
    assert options.max_attempts == 1 and options.validation_runs == 3
    assert options.base_image == "registry.local/python:3.12"
    assert (options.limits.run_timeout, options.limits.build_timeout) == (45, 300)
    assert options.report_id == "abc" and options.vulnerable == RevisionSpec.worktree()

    stub.regression_tests_fixed_ref = str(tmp_path)
    assert PipelineOptions.from_settings(stub, str(tmp_path)).remediation.kind == "directory"


# ── TUI ──

def _tui_app():
    from openhack.tui import OpenHackApp

    app = OpenHackApp.__new__(OpenHackApp)
    app._invalidate = lambda: None
    return app


def test_tui_config_accepts_regression_settings(monkeypatch):
    import openhack.tui as tui

    saved = {}
    monkeypatch.setattr(tui, "save_user_config", saved.update)
    monkeypatch.setattr(tui, "reload_settings", lambda: None)
    app = _tui_app()
    app._cmd_config("regression_tests_enabled true")
    assert saved == {"regression_tests_enabled": "true"}
    assert app.last_status_line == "saved regression_tests_enabled"
    assert any(key == "regression_tests_fixed_ref" for key, _ in tui._CONFIG_KEYS)


def test_tui_restores_regression_status_from_saved_reports():
    from openhack.tui import OpenHackApp

    original = verified_idor_finding(regression_test={"status": "remediation_unavailable"})
    loaded = OpenHackApp._findings_from_report({"findings": [original.to_dict()]})[0]
    assert loaded.regression_test == {"status": "remediation_unavailable"}
    assert loaded.exploit_evidence == original.exploit_evidence
    assert loaded.source == "sandbox_verified"


@pytest.mark.parametrize("status,included,phrase", [
    ("validated", True, "fails on the vulnerable code and passes once"),
    ("remediation_unavailable", True, "must pass after your fix"),
    ("validation_failed", False, None),
])
def test_copy_prompt_carries_the_regression_test(status, included, phrase):
    from openhack.tui import OpenHackApp

    finding = verified_idor_finding(regression_test={
        "status": status, "test_path": TEST_PATH, "test_code": "def test_x():\n    assert True\n",
    })
    prompt = OpenHackApp._format_finding_for_agent(finding)
    assert ("## Regression test" in prompt) is included
    if included:
        assert phrase in prompt
        assert f"Add it unchanged at `{TEST_PATH}` in its test project" in prompt
        assert "```python\ndef test_x():\n    assert True\n```" in prompt


def test_copy_prompt_places_a_subproject_test_from_the_repository_root(tmp_path):
    """A monorepo finding: the test belongs in the project, not at the repository root."""
    import shutil

    from openhack.tui import OpenHackApp, _regression_test_fragments
    from tests.regression_support import FIXTURE_APP

    repo = tmp_path / "monorepo"
    shutil.copytree(FIXTURE_APP / "vulnerable", repo / "services" / "api")
    (repo / "README.md").write_text("# monorepo\n")
    finding = verified_idor_finding(file_path="services/api/app.py")
    runner = FakeRunner()
    pipeline = pipeline_mod.RegressionTestPipeline(
        ScriptedLLM([submit()]), Session(str(repo), persist_events=False), repo,
        PipelineOptions(artifacts_root=tmp_path / "artifacts"), runner=runner,
    )
    result = asyncio.run(pipeline.run([finding]))[0]

    assert result.status.value == "remediation_unavailable"
    assert result.test_path == TEST_PATH
    assert result.artifacts["repo_path"] == f"services/api/{TEST_PATH}"
    assert result.run_command == f"cd services/api && python -m pytest {TEST_PATH}"
    assert list(runner.runs[0]["files"]) == [f"services/api/{TEST_PATH}"]

    prompt = OpenHackApp._format_finding_for_agent(finding)
    assert f"Add it unchanged at `services/api/{TEST_PATH}` (relative to the repository root)" in prompt
    details = "".join(fragment[1] for fragment in _regression_test_fragments(finding))
    assert f"services/api/{TEST_PATH}" in details


def test_tui_details_and_trace_show_regression_status():
    from openhack.tui import ScanState, _regression_test_fragments

    finding = verified_idor_finding(regression_test={
        "status": "validated", "status_label": "generated and validated", "test_path": TEST_PATH,
        "reason": "Fails on the vulnerable revision and passes on the remediated revision.",
        "run_command": f"python -m pytest {TEST_PATH}", "test_code": "def test_x():\n    assert True\n",
        "artifacts": {"patch": "/p/regression-test.patch"},
    })
    text = "".join(fragment[1] for fragment in _regression_test_fragments(finding))
    assert "REGRESSION TEST" in text and "✓ generated and validated" in text
    assert TEST_PATH in text and "patch: /p/regression-test.patch" in text
    assert _regression_test_fragments(verified_idor_finding()) == []

    scan = ScanState(target="/repo")
    scan.update_from_trace(TraceEntry(
        timestamp=time.time(), agent="regression_tests", event_type="regression_test_result",
        content={"title": "IDOR in app.py", "status": "validated", "label": "generated and validated"},
    ))
    rendered = "".join(fragment[1] for _, line in scan.trace_lines for fragment in line)
    assert "generated and validated" in rendered and "IDOR in app.py" in rendered
