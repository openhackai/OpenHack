"""End to end: a verified IDOR becomes a sandbox-validated regression test.

This drives the real workflow — a saved scan report, `openhack
--regression-tests`, revision workspaces, Docker image builds, locked-down
test containers, validation, and the report/patch it writes — against the
intentionally vulnerable notes app and its fix. Only the LLM is scripted: it
first submits a plausible test that checks authentication rather than
ownership (which must be rejected), then a proper invariant test.

It then shows the accepted test is independent of OpenHack: after `git
apply`, the repository's own `python -m pytest` passes on the fixed code and
fails on the vulnerable code.

Requires a Docker daemon plus network access for image builds, so it only runs
with OPENHACK_DOCKER_TESTS=1. OPENHACK_TEST_BASE_IMAGE overrides the base
image (for example one that trusts a corporate TLS root).
"""

import asyncio
import contextlib
import io
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import pytest

from openhack.agents.session import Session
from openhack.config import settings
from openhack.regression import headless
from openhack.regression.models import RegressionTestStatus as Status
from openhack.regression.pipeline import PipelineOptions, RegressionTestPipeline
from openhack.regression.workspace import RevisionSpec
from openhack.sandbox.container_runner import ContainerLimits, ContainerTestRunner
from tests.regression_support import (
    FIX_BRANCH,
    GOOD_INVARIANT_TESTS,
    TEST_PATH,
    ScriptedLLM,
    git,
    install_scan_stubs,
    make_fixture_repo,
    read_file_call,
    submit,
    submit_weak,
    verified_idor_finding,
)


def _docker_ready() -> bool:
    if os.environ.get("OPENHACK_DOCKER_TESTS") != "1" or not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "version"], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(
    not _docker_ready(), reason="set OPENHACK_DOCKER_TESTS=1 and run a Docker daemon",
)
BASE_IMAGE = os.environ.get("OPENHACK_TEST_BASE_IMAGE", "python:3.12-slim")


def _docker(*args: str, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def _run_repo_suite(checkout: Path) -> subprocess.CompletedProcess:
    """Run the repository's own test command in a plain container — no OpenHack code involved."""
    (checkout / "Dockerfile.ci").write_text(
        f"FROM {BASE_IMAGE}\nWORKDIR /app\nCOPY requirements.txt .\n"
        "RUN pip install --no-cache-dir -r requirements.txt\nCOPY . .\n"
    )
    tag = f"openhack-e2e-ci:{uuid4().hex[:10]}"
    build = _docker("build", "-q", "-t", tag, "-f", str(checkout / "Dockerfile.ci"), str(checkout))
    assert build.returncode == 0, build.stderr
    try:
        return _docker("run", "--rm", "--network", "none", tag,
                       "python", "-m", "pytest", "-q", "-p", "no:cacheprovider", timeout=300)
    finally:
        _docker("rmi", "-f", tag)


def _checkout(repo: Path, ref: str, dest: Path) -> Path:
    subprocess.run(["git", "clone", "-q", str(repo), str(dest)], check=True)
    git(dest, "checkout", "-q", ref)
    return dest


def test_verified_idor_becomes_a_validated_regression_test(tmp_path, monkeypatch):
    fx = make_fixture_repo(tmp_path)
    finding = verified_idor_finding()
    scan_id = "e2e00000-0000-4000-8000-000000000001"
    scans = tmp_path / "scans"
    scans.mkdir()
    report_path = scans / f"{scan_id}.json"
    report_path.write_text(json.dumps({
        "scan_id": scan_id,
        "target_dir": str(fx["repo"]),
        "status": "completed",
        "repository": {"head": fx["vulnerable_sha"], "branch": "main", "dirty": False},
        "findings": [finding.to_dict()],
    }))

    llm = ScriptedLLM([read_file_call(), submit_weak(), submit(call_id="submit-good")])
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr(headless, "SCANS_DIR", scans)
    monkeypatch.setattr(headless, "reload_settings", lambda: None)
    monkeypatch.setattr("openhack.agents.llm.LLMClient", lambda *a, **k: llm)
    monkeypatch.setattr(settings, "regression_tests_base_image", BASE_IMAGE)
    monkeypatch.setattr(settings, "regression_tests_fixed_ref", None)
    monkeypatch.setattr(settings, "regression_tests_validation_runs", 2)

    out = io.StringIO()
    started = time.monotonic()
    with contextlib.redirect_stdout(out):
        code = asyncio.run(headless.run_regression_tests_for_report(scan_id, FIX_BRANCH))
    print(out.getvalue())
    print(f"regression workflow took {time.monotonic() - started:.1f}s")

    assert code == 0, out.getvalue()
    status = json.loads(report_path.read_text())["findings"][0]["regressionTest"]
    assert status["status"] == "validated", status["reason"]
    assert status["attempts"] == 2  # the weak test was rejected first
    vulnerable, remediated = status["validation"]["vulnerable"], status["validation"]["remediated"]
    assert vulnerable["resolved"] == fx["vulnerable_sha"]
    assert remediated["resolved"] == fx["fixed_sha"]
    assert {name for name, o in vulnerable["outcomes"].items() if o == "failed"} == set(GOOD_INVARIANT_TESTS)
    assert vulnerable["outcomes"]["test_owner_can_read_own_note"] == "passed"
    assert set(remediated["outcomes"].values()) == {"passed"}
    assert "every invariant test PASSED on the VULNERABLE code" in next(
        m.content for m in reversed(llm.requests[2]) if m.role == "user"
    )
    patch = Path(status["artifacts"]["patch"])
    assert patch.is_file() and Path(status["artifacts"]["test_file"]).read_text() == status["test_code"]
    # Nothing was left behind in Docker.
    leftovers = _docker("ps", "-a", "--filter", "label=openhack.regression=1", "-q").stdout.strip()
    assert leftovers == ""

    # Independence: with the patch applied, the repository's normal test
    # runner — not OpenHack — passes on the fix and fails on the vulnerable code.
    fixed = _checkout(fx["repo"], FIX_BRANCH, tmp_path / "ci-fixed")
    vuln = _checkout(fx["repo"], fx["vulnerable_sha"], tmp_path / "ci-vulnerable")
    for checkout in (fixed, vuln):
        subprocess.run(["git", "-C", str(checkout), "apply", str(patch)], check=True)

    fixed_run = _run_repo_suite(fixed)
    print(fixed_run.stdout)
    assert fixed_run.returncode == 0, fixed_run.stdout
    assert "6 passed" in fixed_run.stdout

    vuln_run = _run_repo_suite(vuln)
    print(vuln_run.stdout)
    assert vuln_run.returncode == 1
    assert "3 failed, 3 passed" in vuln_run.stdout
    for name in GOOD_INVARIANT_TESTS:
        assert f"{TEST_PATH}::{name}" in vuln_run.stdout


def test_headless_scan_with_regression_tests_enabled(tmp_path, monkeypatch):
    """`openhack --scan` with regression tests enabled: Step 6 validates in Docker."""
    import openhack.headless_scan as headless_scan

    fx = make_fixture_repo(tmp_path)
    llm = ScriptedLLM([submit()])
    install_scan_stubs(monkeypatch, llm)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr(headless_scan, "SCANS_DIR", tmp_path / "scans")
    # run_headless_scan reloads settings, so configure it the way a user would.
    for key, value in {
        "REGRESSION_TESTS_ENABLED": "true", "REGRESSION_TESTS_FIXED_REF": FIX_BRANCH,
        "REGRESSION_TESTS_BASE_IMAGE": BASE_IMAGE, "SANDBOX_ENABLED": "true",
        "FEATURE_HUNT_ENABLED": "false", "CHECKPOINT_ENABLED": "false",
    }.items():
        monkeypatch.setenv(key, value)
    snapshot = {name: getattr(settings, name) for name in type(settings).model_fields}
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            asyncio.run(headless_scan.run_headless_scan(str(fx["repo"])))
    finally:
        for name, value in snapshot.items():
            setattr(settings, name, value)
    print(out.getvalue())

    report = json.loads(next((tmp_path / "scans").glob("*.json")).read_text())
    assert report["status"] == "completed"
    assert report["repository"]["head"] == fx["vulnerable_sha"]
    finding = report["findings"][0]
    assert finding["verificationSource"] == "sandbox_verified"
    assert finding["exploitEvidence"]["exploit_request"]["headers"]["Authorization"] == "[REDACTED]"
    status = finding["regressionTest"]
    assert status["status"] == "validated", status["reason"]
    assert status["validation"]["remediated"]["resolved"] == fx["fixed_sha"]
    assert "regression_tests" in report["cost"]["steps"]
    assert "Regression test: generated and validated" in out.getvalue()
    assert "REGRESSION TEST: generated and validated" in out.getvalue()


def test_incomplete_fix_is_reported_not_weakened(tmp_path):
    fx = make_fixture_repo(tmp_path)
    repo = fx["repo"]
    # A "fix" that only protects reads: updates and deletes stay vulnerable.
    git(repo, "checkout", "-q", "-b", "fix/reads-only", fx["vulnerable_sha"])
    app = (repo / "app.py").read_text()
    partial = app.replace(
        "    def get_note(note_id):\n        current_user_id()\n        note = notes.get(note_id)\n"
        "        if note is None:\n",
        "    def get_note(note_id):\n        user_id = current_user_id()\n        note = notes.get(note_id)\n"
        "        if note is None or note[\"owner_id\"] != user_id:\n",
    )
    assert partial != app
    (repo / "app.py").write_text(partial)
    git(repo, "commit", "-q", "-am", "Check ownership on reads")
    git(repo, "checkout", "-q", "main")

    llm = ScriptedLLM([submit(), submit(call_id="must-not-be-requested")])
    pipeline = RegressionTestPipeline(
        llm, Session(str(repo), persist_events=False), repo,
        PipelineOptions(remediation=RevisionSpec.git("fix/reads-only"), base_image=BASE_IMAGE,
                        artifacts_root=tmp_path / "artifacts"),
    )
    result = asyncio.run(pipeline.run([verified_idor_finding()]))[0]

    assert result.status is Status.VALIDATION_FAILED
    assert "remediation is incomplete" in result.reason
    assert "test_other_user_cannot_modify_note" in result.reason
    assert "test_other_user_cannot_delete_note" in result.reason
    assert result.validation["remediated"]["outcomes"]["test_other_user_cannot_read_note"] == "passed"
    assert llm.calls == 1  # never asked to "fix" the test by dropping coverage
    assert result.artifacts == {}


def test_projects_with_editable_requirements_constraints_and_a_coverage_gate(tmp_path):
    """Common real-world tooling must not stop validation: `-e .` in a
    requirements file, a constraints file pinning a package the project never
    installs (and that does not exist), and a coverage threshold in addopts
    that a single-file run can never meet."""
    def tooling(repo: Path) -> None:
        (repo / "requirements.txt").write_text("-c constraints.txt\nflask>=3.0,<4\n")
        (repo / "constraints.txt").write_text("flask<4\nopenhack-unpublished-internal-package==1.0\n")
        (repo / "requirements-dev.txt").write_text("-r requirements.txt\n-e .\npytest>=8.0\npytest-cov>=4\n")
        (repo / "setup.py").write_text(
            "from setuptools import setup\n\nsetup(name='notes-app', version='0.0.0', py_modules=['app'])\n"
        )
        with open(repo / "pytest.ini", "a") as ini:
            ini.write("addopts = --cov=app --cov-fail-under=100\n")

    fx = make_fixture_repo(tmp_path, customize=tooling)
    repo = fx["repo"]
    llm = ScriptedLLM([submit()])
    pipeline = RegressionTestPipeline(
        llm, Session(str(repo), persist_events=False), repo,
        PipelineOptions(remediation=RevisionSpec.git(FIX_BRANCH), base_image=BASE_IMAGE,
                        artifacts_root=tmp_path / "artifacts"),
    )
    result = asyncio.run(pipeline.run([verified_idor_finding()]))[0]

    assert result.status is Status.VALIDATED, result.reason
    assert result.attempts == 1
    assert {n for n, o in result.validation["vulnerable"]["outcomes"].items() if o == "failed"} == set(GOOD_INVARIANT_TESTS)


def _probe_image(tmp_path: Path, runner: ContainerTestRunner) -> str:
    context = tmp_path / "probe"
    context.mkdir()
    tag = f"openhack-regression:probe-{uuid4().hex[:8]}"
    asyncio.run(runner.build_image(
        context,
        f"FROM {BASE_IMAGE}\nRUN mkdir -p /openhack-results /workspace && "
        "chown -R 65534:65534 /openhack-results /workspace\n",
        tag,
    ))
    return tag


def test_test_containers_are_locked_down(tmp_path):
    runner = ContainerTestRunner(ContainerLimits(run_timeout=60))
    image = _probe_image(tmp_path, runner)
    try:
        script = (
            "id -u > /openhack-results/uid; "
            "cat /workspace/tests/staged.txt > /openhack-results/staged; "
            "grep CapEff /proc/self/status > /openhack-results/caps; "
            "python -c \"import socket; socket.create_connection(('1.1.1.1', 53), 3)\" "
            "> /openhack-results/net 2>&1; echo $? > /openhack-results/net_rc; "
            "touch /openhack-results/done"
        )
        run = asyncio.run(runner.run_test(image, {"tests/staged.txt": "staged-by-openhack"}, ["sh", "-c", script]))
        try:
            results = run.results_dir
            assert run.exit_code == 0 and not run.timed_out
            assert (results / "uid").read_text().strip() == "65534"
            assert (results / "staged").read_text() == "staged-by-openhack"
            assert (results / "caps").read_text().split()[-1] == "0000000000000000"
            assert (results / "net_rc").read_text().strip() != "0"
            assert "unreachable" in (results / "net").read_text().lower()
        finally:
            run.cleanup()
        assert _docker("ps", "-a", "--filter", f"name={run.container}", "-q").stdout.strip() == ""
    finally:
        asyncio.run(runner.remove_images())


def test_results_are_capped_and_links_never_reach_the_host(tmp_path):
    mb = 1024 * 1024
    runner = ContainerTestRunner(ContainerLimits(run_timeout=60, file_size=4 * mb, results_bytes=mb))
    image = _probe_image(tmp_path, runner)
    try:
        script = (
            "ln -s /etc/hostname /openhack-results/output.txt; "
            "ulimit -f > /openhack-results/fsize; "
            "head -c 8000000 /dev/zero > /tmp/big; echo $? > /openhack-results/big_rc; "
            "wc -c < /tmp/big > /openhack-results/big_size; "
            "echo '<testsuites/>' > /openhack-results/junit.xml"
        )
        run = asyncio.run(runner.run_test(image, {}, ["sh", "-c", script]))
        try:
            assert run.results_error is None
            # The planted symlink stays in the container.
            assert sorted(p.name for p in run.results_dir.iterdir()) == ["big_rc", "big_size", "fsize", "junit.xml"]
            assert (run.results_dir / "fsize").read_text().strip() == str(4 * mb // 512)
            assert (run.results_dir / "big_rc").read_text().strip() != "0"  # killed at the file-size limit
            assert int((run.results_dir / "big_size").read_text()) == 4 * mb
        finally:
            run.cleanup()

        flood = asyncio.run(runner.run_test(
            image, {}, ["sh", "-c", "head -c 3000000 /dev/zero > /openhack-results/flood"],
        ))
        try:
            assert "more than 1 MB in its results directory" in flood.results_error
            assert list(flood.results_dir.iterdir()) == []
        finally:
            flood.cleanup()
        assert _docker("ps", "-a", "--filter", f"name={flood.container}", "-q").stdout.strip() == ""
    finally:
        asyncio.run(runner.remove_images())


def test_hanging_tests_are_killed_at_the_timeout(tmp_path):
    runner = ContainerTestRunner(ContainerLimits(run_timeout=5))
    image = _probe_image(tmp_path, runner)
    try:
        started = time.monotonic()
        run = asyncio.run(runner.run_test(image, {}, ["sleep", "300"]))
        run.cleanup()
        assert run.timed_out and run.exit_code is None
        assert time.monotonic() - started < 60
        assert _docker("ps", "-a", "--filter", f"name={run.container}", "-q").stdout.strip() == ""
    finally:
        asyncio.run(runner.remove_images())
