"""
Security regression test generation for verified findings.

For every finding the workflow looks at:

    eligibility ─► invariant ─► generate (LLM) ─► path + static policy
      ─► sandbox, vulnerable revision: control tests pass and an invariant
         test fails on an assertion (N runs, identical outcomes)
      ─► sandbox, remediated revision: every test passes (N runs)
      ─► accept: attach to the finding, write the test + a patch artifact

Rejections are fed back to the generator for another attempt. A test is never
accepted unless it was shown to fail *because the vulnerability exists* and to
pass once it is fixed. Without a remediated revision, a test that reproduces
the vulnerability is kept as a pending artifact (``remediation_unavailable``)
and can be re-validated later without another LLM call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Optional
from uuid import uuid4

from openhack.agents.session import Finding, Session
from openhack.sandbox.container_runner import ContainerLimits, ContainerTestRunner
from openhack.sandbox.orchestrator import SandboxError
from openhack.tools.registry import ToolRegistry
from .frameworks import (
    FrameworkAdapter, FrameworkProfile, RunReport, UnsupportedFrameworkError, detect_framework,
)
from .generator import RegressionTestGeneratorAgent, CandidateTest
from .invariants import (
    MissingEvidenceError, VulnerabilityClassSpec, spec_for_finding, supported_classes,
)
from .models import RegressionTestResult, RegressionTestStatus, SecurityInvariant
from .workspace import (
    MaterializedRevision, RevisionSpec, WorkspaceError, git_info, materialize,
    new_file_patch, remove_tree, write_private_file,
)

logger = logging.getLogger(__name__)

AGENT = "regression_tests"
Status = RegressionTestStatus


@dataclass
class PipelineOptions:
    vulnerable: RevisionSpec = field(default_factory=RevisionSpec.worktree)
    remediation: Optional[RevisionSpec] = None
    max_attempts: int = 3
    validation_runs: int = 2
    base_image: str = ""
    limits: ContainerLimits = field(default_factory=ContainerLimits)
    artifacts_root: Optional[Path] = None
    # Session quoted in re-validation hints and used to group artifacts.
    report_id: Optional[str] = None

    @classmethod
    def from_settings(cls, settings, target_dir: str, **overrides: Any) -> "PipelineOptions":
        remediation = None
        if settings.regression_tests_fixed_ref:
            remediation = RevisionSpec.parse(settings.regression_tests_fixed_ref, Path(target_dir))
        options = cls(
            remediation=remediation,
            max_attempts=max(1, int(settings.regression_tests_max_attempts)),
            validation_runs=max(1, int(settings.regression_tests_validation_runs)),
            base_image=settings.regression_tests_base_image or "",
            limits=ContainerLimits(
                run_timeout=int(settings.regression_tests_timeout),
                build_timeout=int(settings.regression_tests_build_timeout),
            ),
        )
        for key, value in overrides.items():
            setattr(options, key, value)
        return options


@dataclass
class RevisionCheck:
    passed: bool
    detail: str
    # False when retrying cannot help without weakening the test.
    retryable: bool = True
    deterministic: bool = True
    outcomes: dict[str, str] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)
    output_tail: str = ""

    def to_dict(self, revision: MaterializedRevision) -> dict:
        return {
            "revision": revision.label,
            "resolved": revision.resolved,
            "passed": self.passed,
            "deterministic": self.deterministic,
            "outcomes": {k.split("::")[-1]: v for k, v in self.outcomes.items()},
            "detail": self.detail,
        }


def evaluate_runs(
    reports: list[RunReport],
    submission: CandidateTest,
    expect: str,
    require_control: bool,
    timeout: int,
) -> RevisionCheck:
    """Judge repeated runs of a candidate test against one revision.

    ``expect="vulnerable"``: controls pass and at least one invariant test
    fails on an assertion. ``expect="remediated"``: every test passes.
    """
    first = reports[0]
    outcomes = first.outcomes()
    failures = {c.name: c.message for c in first.cases if c.outcome != "passed" and c.message}
    deterministic = all(r.outcomes() == outcomes for r in reports[1:])

    def result(passed: bool, detail: str, retryable: bool = True) -> RevisionCheck:
        return RevisionCheck(passed, detail, retryable, deterministic, outcomes, failures, first.output_tail)

    if any(r.timed_out for r in reports):
        return result(False, f"the test run timed out after {timeout}s (does it block, sleep or need the network?)")
    errors = [r.collection_error for r in reports if r.collection_error]
    if errors:
        return result(False, f"the test could not be collected or run: {errors[0][:1500]}")
    if not deterministic:
        changed = sorted(
            k.split("::")[-1] for r in reports[1:] for k in set(outcomes) | set(r.outcomes())
            if outcomes.get(k) != r.outcomes().get(k)
        )
        return result(
            False,
            f"outcomes differed between {len(reports)} identical runs "
            f"({', '.join(dict.fromkeys(changed))}); the test is nondeterministic",
        )

    names = {c.base_name for c in first.cases}
    invariant = set(submission.invariant_tests)
    control = set(submission.control_tests)
    if not invariant:
        return result(False, "no invariant tests were declared")
    if require_control and not control:
        return result(False, "at least one control test (legitimate access still works) is required")
    if invariant & control:
        return result(False, f"tests declared as both invariant and control: {', '.join(sorted(invariant & control))}")
    missing = (invariant | control) - names
    if missing:
        return result(False, f"declared tests were not collected: {', '.join(sorted(missing))}")
    undeclared = names - invariant - control
    if undeclared:
        return result(False, f"tests neither declared invariant nor control: {', '.join(sorted(undeclared))}")
    broken = [c for c in first.cases if c.outcome in ("error", "skipped")]
    if broken:
        return result(False, "tests errored or were skipped: " + ", ".join(f"{c.name} ({c.outcome})" for c in broken))

    if expect == "vulnerable":
        failed_controls = [c.name for c in first.cases if c.base_name in control and c.outcome != "passed"]
        if failed_controls:
            return result(
                False,
                f"control test(s) failed on the VULNERABLE code ({', '.join(failed_controls)}), so the "
                "failure is not caused by the vulnerability — the legitimate flow must work on both versions",
            )
        failed = [c for c in first.cases if c.base_name in invariant and c.outcome == "failed"]
        if not failed:
            return result(
                False,
                "every invariant test PASSED on the VULNERABLE code, so the test does not detect the "
                "vulnerability; assert the security property the exploit violated",
            )
        crashed = [c.name for c in failed if not c.assertion]
        if crashed:
            return result(
                False,
                f"invariant test(s) {', '.join(crashed)} failed with an unexpected exception rather than "
                "an assertion, so it is not the security property that failed",
            )
        return result(
            True,
            f"{len(failed)} invariant test(s) failed on assertions while {len(control)} control test(s) passed",
        )

    failing = [c for c in first.cases if c.outcome != "passed"]
    if failing:
        invariant_assertions = [
            c.name for c in failing if c.base_name in invariant and c.outcome == "failed" and c.assertion
        ]
        if invariant_assertions:
            return result(
                False,
                f"invariant test(s) {', '.join(invariant_assertions)} still fail their assertions on the "
                "REMEDIATED code: either the remediation is incomplete for that operation or the test "
                "pins a detail of one particular fix. OpenHack does not weaken a test to make it pass — "
                "review the fix and the test",
                retryable=False,
            )
        return result(
            False,
            "test(s) failed on the REMEDIATED code: " + ", ".join(c.name for c in failing)
            + ". The test setup must work against the fixed code too",
        )
    return result(True, f"all {len(first.cases)} test(s) passed")


def _feedback(stage: str, check: RevisionCheck) -> str:
    lines = [f"Stage: {stage}", f"Problem: {check.detail}"]
    if check.outcomes:
        lines.append("Outcomes: " + ", ".join(
            f"{name.split('::')[-1]}={outcome}" for name, outcome in check.outcomes.items()
        ))
    for name, message in list(check.failures.items())[:6]:
        lines.append(f"--- {name} ---\n{message[-800:]}")
    if not check.outcomes and check.output_tail:
        lines.append(f"Test output (tail):\n{check.output_tail[-1500:]}")
    lines.append(
        "Keep every invariant test that covers an operation on the object; fix setup or "
        "assertions that pin implementation details instead of removing coverage."
    )
    return "\n".join(lines)


def _with_provenance(code: str, invariant: SecurityInvariant) -> str:
    """Normalize line endings and credit the test's origin in a header comment."""
    code = code.replace("\r\n", "\n")
    if not code.endswith("\n"):
        code += "\n"
    if "openhack" in code[:800].lower():
        return code
    header = [
        "# Security regression test generated by OpenHack from a sandbox-verified finding.",
        f"# Invariant ({invariant.cwe}):",
        *[f"#   {line}" for line in textwrap.wrap(invariant.statement, 76)],
    ]
    lines = code.split("\n")
    insert_at = 0
    while insert_at < min(2, len(lines)) and (
        lines[insert_at].startswith("#!") or re.match(r"#.*coding[:=]", lines[insert_at])
    ):
        insert_at += 1
    return "\n".join(lines[:insert_at] + header + lines[insert_at:])


@dataclass
class _Verdict:
    ok: bool
    pending: bool
    retryable: bool
    reason: str
    feedback: str
    validation: dict


class _ImageCache:
    """One sandbox image per (revision, project root), shared by all findings."""

    def __init__(self, runner: ContainerTestRunner, base_image: str, run_id: str):
        self.runner = runner
        self.base_image = base_image
        self.run_id = run_id
        self._tags: dict[tuple[str, str], str] = {}
        self._errors: dict[tuple[str, str], SandboxError] = {}

    async def get(self, revision: MaterializedRevision, label: str,
                  adapter: FrameworkAdapter, profile: FrameworkProfile) -> str:
        key = (label, profile.project_root)
        if key in self._errors:
            raise self._errors[key]
        if key not in self._tags:
            tag = f"openhack-regression:{self.run_id}-{label}-{len(self._tags)}"
            base = self.base_image or adapter.default_base_image(profile)
            try:
                await self.runner.build_image(revision.root, adapter.dockerfile(profile, base), tag)
            except SandboxError as exc:
                self._errors[key] = exc
                raise
            self._tags[key] = tag
        return self._tags[key]


def default_artifacts_root(session: Session) -> Path:
    if session.event_log_path:
        return Path(session.event_log_path).parent / "artifacts"
    return Path.home() / ".openhack" / "scans" / "artifacts"


class RegressionTestPipeline:
    """Runs the regression-test workflow for a list of findings."""

    def __init__(
        self,
        llm,
        session: Session,
        target_dir: str | Path,
        options: Optional[PipelineOptions] = None,
        runner: Optional[ContainerTestRunner] = None,
    ):
        self.llm = llm
        self.session = session
        self.target_dir = Path(target_dir).resolve()
        self.options = options or PipelineOptions()
        self.runner = runner or ContainerTestRunner(self.options.limits)
        self.run_id = uuid4().hex[:10]
        self.git = git_info(self.target_dir)

    @property
    def total_cost(self) -> float:
        return float(getattr(self.llm, "total_cost", 0.0) or 0.0)

    async def run(self, findings: list[Finding]) -> list[RegressionTestResult]:
        results: dict[str, RegressionTestResult] = {}
        eligible: list[tuple[int, Finding, VulnerabilityClassSpec, SecurityInvariant]] = []
        for index, finding in enumerate(findings):
            spec = spec_for_finding(finding)
            if spec is None:
                results[finding.id] = self._result(
                    finding, Status.UNSUPPORTED_VULNERABILITY,
                    f"{finding.category or 'This category'} is not supported yet "
                    f"(supported: {', '.join(supported_classes())})",
                )
                continue
            try:
                invariant = spec.extract_invariant(finding)
            except MissingEvidenceError as exc:
                results[finding.id] = self._result(finding, Status.MISSING_EVIDENCE, str(exc), spec)
                continue
            eligible.append((index, finding, spec, invariant))

        if eligible:
            self.session.add_trace(
                agent=AGENT, event_type="status",
                content=f"Generating regression tests for {len(eligible)} eligible finding(s)",
            )
            try:
                await self._run_eligible(eligible, results)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Never leave an eligible finding without a status.
                logger.warning(f"Regression test workflow failed: {exc}", exc_info=True)
                for _, finding, spec, invariant in eligible:
                    results.setdefault(finding.id, self._result(
                        finding, Status.GENERATION_FAILED,
                        f"the regression test workflow failed: {exc}", spec, invariant,
                    ))

        ordered = []
        for finding in findings:
            result = results[finding.id]
            finding.regression_test = result.to_dict()
            self.session.add_trace(
                agent=AGENT, event_type="regression_test_result",
                content={
                    "title": finding.title,
                    "status": result.status.value,
                    "label": result.status.label,
                    "reason": result.reason,
                    "test_path": result.test_path or "",
                },
            )
            ordered.append(result)
        return ordered

    async def _run_eligible(self, eligible, results: dict[str, RegressionTestResult]) -> None:
        work_dir = Path(tempfile.mkdtemp(prefix="openhack-regression-"))
        try:
            try:
                vulnerable = await asyncio.to_thread(
                    materialize, self.target_dir, self.options.vulnerable, work_dir / "vulnerable"
                )
            except WorkspaceError as exc:
                for _, finding, spec, invariant in eligible:
                    results[finding.id] = self._result(
                        finding, Status.SANDBOX_ERROR,
                        f"could not prepare the vulnerable revision "
                        f"({self.options.vulnerable.describe()}): {exc}", spec, invariant,
                    )
                return
            remediated, remediation_note = await self._prepare_remediated(work_dir, vulnerable)
            try:
                await self.runner.ensure_available()
            except SandboxError as exc:
                for _, finding, spec, invariant in eligible:
                    results[finding.id] = self._result(finding, Status.SANDBOX_ERROR, str(exc), spec, invariant)
                return

            images = _ImageCache(self.runner, self.options.base_image, self.run_id)
            for index, finding, spec, invariant in eligible:
                if self.session.cancelled:
                    results[finding.id] = self._result(
                        finding, Status.GENERATION_FAILED,
                        "cancelled before a regression test was generated", spec, invariant,
                    )
                    continue
                try:
                    adapter, profile = detect_framework(vulnerable.root, finding.file_path)
                except UnsupportedFrameworkError as exc:
                    results[finding.id] = self._result(
                        finding, Status.UNSUPPORTED_FRAMEWORK, str(exc), spec, invariant,
                    )
                    continue
                results[finding.id] = await self._process(
                    index, finding, spec, invariant, adapter, profile,
                    vulnerable, remediated, remediation_note, images,
                )
        finally:
            await self.runner.remove_images()
            await asyncio.to_thread(remove_tree, work_dir)

    async def _prepare_remediated(
        self, work_dir: Path, vulnerable: MaterializedRevision,
    ) -> tuple[Optional[MaterializedRevision], str]:
        spec = self.options.remediation
        hint = f"openhack --regression-tests {self.options.report_id or self.session.id} <fixed-revision>"
        if spec is None:
            return None, (
                "no remediated revision was available to validate against (set "
                f"regression_tests_fixed_ref, or re-validate after fixing with `{hint}`)"
            )
        try:
            remediated = await asyncio.to_thread(
                materialize, self.target_dir, spec, work_dir / "remediated"
            )
        except WorkspaceError as exc:
            return None, f"the remediated revision could not be prepared ({spec.describe()}): {exc}"
        if remediated.fingerprint == vulnerable.fingerprint:
            return None, (
                f"the remediated revision ({remediated.label}) is identical to the vulnerable "
                f"revision — commit the fix and re-validate with `{hint}`"
            )
        return remediated, ""

    @staticmethod
    def _stored_submission(finding: Finding) -> Optional[CandidateTest]:
        """A previously generated test that already reproduced the vulnerability."""
        data = finding.regression_test
        if not isinstance(data, dict):
            return None
        if data.get("status") not in (Status.VALIDATED.value, Status.REMEDIATION_UNAVAILABLE.value):
            return None
        if not data.get("test_code") or not data.get("test_path") or not data.get("invariant_tests"):
            return None
        return CandidateTest(
            path=data["test_path"], code=data["test_code"],
            invariant_tests=list(data.get("invariant_tests") or []),
            control_tests=list(data.get("control_tests") or []),
            summary=data.get("summary") or "",
        )

    async def _process(
        self, index: int, finding: Finding, spec: VulnerabilityClassSpec,
        invariant: SecurityInvariant, adapter: FrameworkAdapter, profile: FrameworkProfile,
        vulnerable: MaterializedRevision, remediated: Optional[MaterializedRevision],
        remediation_note: str, images: _ImageCache,
    ) -> RegressionTestResult:
        cost_before = self.total_cost
        self.session.add_trace(
            agent=AGENT, event_type="regression_test_started",
            content={"title": finding.title, "class": spec.key,
                     "endpoint": f"{invariant.method} {invariant.path_template}"},
        )
        root = profile.project_root

        def repo_path(rel: str) -> str:
            return rel if root == "." else f"{root}/{rel}"

        def available(rel: str, stored_code: Optional[str] = None) -> bool:
            path = repo_path(rel)
            if vulnerable.exists(path):
                return False
            if remediated is not None and remediated.exists(path):
                # The developer may have committed our pending test alongside
                # the fix; re-validating that identical file is fine.
                existing = (remediated.root / path).read_text(encoding="utf-8", errors="replace")
                return stored_code is not None and existing == stored_code
            return True

        suggested = adapter.suggest_test_path(profile, invariant, lambda rel: not available(rel))

        def outcome(status: RegressionTestStatus, reason: str) -> RegressionTestResult:
            return self._result(finding, status, reason, spec, invariant, adapter.name,
                                attempts, self.total_cost - cost_before)

        candidate = self._stored_submission(finding)
        candidate_is_stored = candidate is not None
        agent: Optional[RegressionTestGeneratorAgent] = None
        generation_note = ""
        feedback = ""
        attempts = 0
        executed = False
        last_status, last_reason, last_validation = Status.GENERATION_FAILED, "no test was generated", {}
        last_candidate: Optional[CandidateTest] = None

        while True:
            if self.session.cancelled:
                return outcome(Status.GENERATION_FAILED, "cancelled")
            if candidate is None:
                if attempts >= self.options.max_attempts:
                    break
                if self.llm is None:
                    last_reason = "no LLM is configured to generate a new test"
                    break
                attempts += 1
                self.session.add_trace(
                    agent=AGENT, event_type="status",
                    content=f"{finding.title}: generation attempt {attempts}/{self.options.max_attempts}",
                )
                try:
                    if agent is None:
                        agent = RegressionTestGeneratorAgent(
                            self.llm, ToolRegistry(target_dir=vulnerable.root), self.session,
                            finding=finding, invariant=invariant, spec=spec, adapter=adapter,
                            profile=profile, suggested_path=suggested, finding_index=index,
                        )
                        submission = await agent.generate(note=generation_note)
                    else:
                        submission = await agent.revise(feedback)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(f"Regression test generator failed: {exc}", exc_info=True)
                    last_status = Status.VALIDATION_FAILED if executed else Status.GENERATION_FAILED
                    last_reason = f"the generator failed: {exc}"
                    break
                if submission is None:
                    feedback = "No test was submitted. Call submit_regression_test with the complete file."
                    if not executed:
                        last_status, last_reason = Status.GENERATION_FAILED, "the generator did not submit a test"
                    continue
                candidate = CandidateTest(
                    path=submission.path,
                    code=_with_provenance(submission.code, invariant),
                    invariant_tests=submission.invariant_tests,
                    control_tests=submission.control_tests,
                    summary=submission.summary,
                )
                candidate_is_stored = False

            last_candidate = candidate
            problems: list[str] = []
            path_error = adapter.validate_test_path(profile, candidate.path)
            if path_error:
                problems.append(path_error)
            elif not available(candidate.path, candidate.code if candidate_is_stored else None):
                problems.append(
                    f"`{candidate.path}` already exists in the repository; existing tests are never "
                    f"modified — use a new file such as `{suggested}`"
                )
            problems += adapter.static_check(candidate.code)
            if problems:
                feedback = "The test was rejected before execution:\n" + "\n".join(f"- {p}" for p in problems)
                last_status = Status.VALIDATION_FAILED if executed else Status.GENERATION_FAILED
                last_reason = "rejected before execution: " + "; ".join(problems[:3])
                if candidate_is_stored:
                    generation_note = f"A previously generated test was unusable: {feedback}"
                candidate = None
                continue

            try:
                verdict = await self._validate(candidate, spec, adapter, profile, vulnerable, remediated, images)
            except SandboxError as exc:
                return outcome(Status.SANDBOX_ERROR, str(exc))
            executed = True
            if verdict.ok or verdict.pending:
                if verdict.ok:
                    assert remediated is not None
                    status = Status.VALIDATED
                    reason = (
                        f"Fails on the vulnerable revision ({vulnerable.label}) and passes on the "
                        f"remediated revision ({remediated.label}) in "
                        f"{self.options.validation_runs}/{self.options.validation_runs} sandbox runs each."
                    )
                else:
                    status = Status.REMEDIATION_UNAVAILABLE
                    reason = (
                        f"The test reproduces the vulnerability (fails on {vulnerable.label} in "
                        f"{self.options.validation_runs}/{self.options.validation_runs} sandbox runs) "
                        f"but was not validated against a fix: {remediation_note}."
                    )
                return self._accept(finding, status, reason, spec, invariant, adapter, profile,
                                    candidate, verdict.validation, attempts, cost_before)

            last_status, last_reason, last_validation = Status.VALIDATION_FAILED, verdict.reason, verdict.validation
            if not verdict.retryable:
                break
            feedback = verdict.feedback
            if candidate_is_stored:
                generation_note = (
                    "A previously generated test for this finding failed re-validation:\n"
                    f"{verdict.feedback}\n\nPrevious test (`{candidate.path}`):\n```\n{candidate.code}\n```\n"
                    "Write a corrected test."
                )
            candidate = None

        result = outcome(last_status, last_reason)
        result.validation = last_validation
        if last_candidate is not None and executed:
            # Keep the rejected test for review, but never as an accepted artifact.
            result.test_path = last_candidate.path
            result.test_code = last_candidate.code
            result.invariant_tests = list(last_candidate.invariant_tests)
            result.control_tests = list(last_candidate.control_tests)
        return result

    async def _validate(
        self, candidate: CandidateTest, spec: VulnerabilityClassSpec,
        adapter: FrameworkAdapter, profile: FrameworkProfile,
        vulnerable: MaterializedRevision, remediated: Optional[MaterializedRevision],
        images: _ImageCache,
    ) -> _Verdict:
        runs = self.options.validation_runs
        timeout = self.options.limits.run_timeout
        validation: dict[str, Any] = {"runs_per_revision": runs, "vulnerable": None, "remediated": None}

        image = await images.get(vulnerable, "vulnerable", adapter, profile)
        reports = [await self._run_once(image, adapter, profile, candidate) for _ in range(runs)]
        check = evaluate_runs(reports, candidate, "vulnerable", spec.requires_control_test, timeout)
        validation["vulnerable"] = check.to_dict(vulnerable)
        self.session.add_trace(
            agent=AGENT, event_type="regression_test_check",
            content={"revision": "vulnerable", "passed": check.passed, "detail": check.detail},
        )
        if not check.passed:
            return _Verdict(False, False, True, f"On the vulnerable revision: {check.detail}",
                            _feedback("vulnerable revision", check), validation)
        if remediated is None:
            return _Verdict(False, True, False, check.detail, "", validation)

        image = await images.get(remediated, "remediated", adapter, profile)
        reports = [await self._run_once(image, adapter, profile, candidate) for _ in range(runs)]
        check = evaluate_runs(reports, candidate, "remediated", spec.requires_control_test, timeout)
        validation["remediated"] = check.to_dict(remediated)
        self.session.add_trace(
            agent=AGENT, event_type="regression_test_check",
            content={"revision": "remediated", "passed": check.passed, "detail": check.detail},
        )
        if not check.passed:
            return _Verdict(False, False, check.retryable, f"On the remediated revision: {check.detail}",
                            _feedback("remediated revision", check), validation)
        return _Verdict(True, False, False, "", "", validation)

    async def _run_once(self, image: str, adapter: FrameworkAdapter,
                        profile: FrameworkProfile, candidate: CandidateTest) -> RunReport:
        root = profile.project_root
        rel_in_repo = candidate.path if root == "." else f"{root}/{candidate.path}"
        workdir = "/workspace" if root == "." else f"/workspace/{root}"
        run = await self.runner.run_test(
            image, {rel_in_repo: candidate.code},
            adapter.container_command(profile, candidate.path), workdir=workdir,
        )
        try:
            return adapter.parse_results(run.results_dir, run.exit_code, run.timed_out)
        finally:
            run.cleanup()

    def _result(
        self, finding: Finding, status: RegressionTestStatus, reason: str,
        spec: Optional[VulnerabilityClassSpec] = None,
        invariant: Optional[SecurityInvariant] = None,
        framework: Optional[str] = None, attempts: int = 0, cost: float = 0.0,
    ) -> RegressionTestResult:
        return RegressionTestResult(
            finding_id=finding.id, status=status, reason=reason,
            vulnerability_class=spec.key if spec else None, invariant=invariant,
            framework=framework, attempts=attempts, cost=round(cost, 6),
            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

    def _accept(
        self, finding: Finding, status: RegressionTestStatus, reason: str,
        spec: VulnerabilityClassSpec, invariant: SecurityInvariant,
        adapter: FrameworkAdapter, profile: FrameworkProfile, candidate: CandidateTest,
        validation: dict, attempts: int, cost_before: float,
    ) -> RegressionTestResult:
        result = self._result(finding, status, reason, spec, invariant, adapter.name,
                              attempts, self.total_cost - cost_before)
        result.test_path = candidate.path
        result.test_code = candidate.code
        result.run_command = adapter.display_command(profile, candidate.path)
        result.invariant_tests = list(candidate.invariant_tests)
        result.control_tests = list(candidate.control_tests)
        result.summary = candidate.summary
        result.validation = validation
        try:
            result.artifacts = self._write_artifacts(finding, result, profile)
        except OSError as exc:
            logger.warning(f"Could not write regression test artifacts: {exc}")
        return result

    def _write_artifacts(self, finding: Finding, result: RegressionTestResult,
                         profile: FrameworkProfile) -> dict[str, str]:
        root = self.options.artifacts_root or default_artifacts_root(self.session)
        session_dir = root / (self.options.report_id or self.session.id)
        art_dir = session_dir / "regression-tests" / finding.id[:8]
        art_dir.mkdir(parents=True, exist_ok=True)
        for directory in (session_dir, art_dir.parent, art_dir):
            try:
                os.chmod(directory, 0o700)
            except OSError:
                pass
        parts = [p for p in (self.git.get("prefix", ""), profile.project_root, result.test_path) if p and p != "."]
        repo_path = PurePosixPath(*parts).as_posix()
        test_file = art_dir / repo_path
        patch = art_dir / "regression-test.patch"
        write_private_file(test_file, result.test_code or "")
        write_private_file(patch, new_file_patch(repo_path, result.test_code or ""))
        artifacts = {
            "dir": str(art_dir),
            "test_file": str(test_file),
            "patch": str(patch),
            "repo_path": repo_path,
            "apply": f"git apply {patch}",
        }
        record = result.to_dict()
        record["artifacts"] = artifacts
        write_private_file(art_dir / "result.json", json.dumps(record, indent=2, default=str))
        return artifacts
