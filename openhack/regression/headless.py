"""
Headless regression-test run for a saved scan (``openhack --regression-tests``).

The usual order of events is: a scan verifies a vulnerability, a developer
fixes it, and only then is there a remediated revision to validate against.
This entry point loads the saved report, uses the commit that was scanned as
the vulnerable revision, validates (or generates) a regression test for each
finding against the fixed revision, and writes the results back into the
report — re-validating an already generated test costs no LLM calls.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from openhack.agents.session import Finding, Session, TraceEntry
from openhack.config import reload_settings, settings
from openhack.headless_scan import SCANS_DIR, format_regression_status
from .pipeline import PipelineOptions, RegressionTestPipeline
from .workspace import RevisionSpec, WorkspaceError, git_info, resolve_commit

logger = logging.getLogger(__name__)


def find_report(session_id: str, scans_dir: Optional[Path] = None) -> Optional[Path]:
    scans_dir = scans_dir or SCANS_DIR
    exact = scans_dir / f"{session_id}.json"
    if exact.is_file():
        return exact
    matches = sorted(scans_dir.glob(f"{session_id}*.json"))
    return matches[0] if matches else None


def choose_revisions(
    report: dict, target: Path, fixed: Optional[str],
) -> tuple[RevisionSpec, Optional[RevisionSpec], list[str]]:
    """Pick the vulnerable and remediated revisions for a saved scan.

    Vulnerable: the commit that was scanned, when the scan ran on a clean tree
    (otherwise the working tree). Remediated: the explicit argument, then the
    regression_tests_fixed_ref setting, then HEAD when it has moved past the
    scanned commit.
    """
    notes: list[str] = []
    repo = report.get("repository") or {}
    info = git_info(target)
    vulnerable = RevisionSpec.worktree()
    scanned = repo.get("head") or ""
    if info.get("is_repo") and scanned and not repo.get("dirty"):
        try:
            resolve_commit(target, scanned)
            vulnerable = RevisionSpec.git(scanned)
        except WorkspaceError:
            notes.append(f"scanned commit {scanned[:12]} is not in this repository; using the working tree")
    elif repo.get("dirty"):
        notes.append("the scan ran on uncommitted changes; using the working tree as the vulnerable revision")
    else:
        notes.append("no scanned commit recorded; using the working tree as the vulnerable revision")

    raw = fixed or settings.regression_tests_fixed_ref
    remediation: Optional[RevisionSpec] = None
    if raw:
        remediation = RevisionSpec.parse(raw, target)
    elif vulnerable.kind == "git" and info.get("head") and info["head"] != scanned:
        remediation = RevisionSpec.git(info["head"])
        notes.append("using HEAD as the remediated revision")
    return vulnerable, remediation, notes


def _write_report_atomically(path: Path, report: dict) -> None:
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w") as fp:
        json.dump(report, fp, indent=2, default=str, ensure_ascii=False)
    os.replace(tmp, path)


def _on_trace(entry: TraceEntry) -> None:
    if entry.event_type == "status":
        print(f"    [{entry.agent}] {str(entry.content or '')[:140]}")
    elif entry.event_type == "regression_test_check":
        data = entry.content if isinstance(entry.content, dict) else {}
        mark = "ok" if data.get("passed") else "rejected"
        print(f"    [{entry.agent}] {data.get('revision')} revision: {mark} — {str(data.get('detail', ''))[:160]}")


async def run_regression_tests_for_report(session_id: str, fixed: Optional[str] = None) -> int:
    """Run the regression-test workflow for a saved scan. Returns an exit status."""
    reload_settings()
    report_path = find_report(session_id)
    if report_path is None:
        print(f"Session {session_id} not found in ~/.openhack/scans/")
        print("List saved sessions with: openhack --sessions")
        return 1
    report = json.loads(report_path.read_text())
    target = Path(report.get("target_dir") or "")
    if not target.is_dir():
        print(f"Error: the scanned directory no longer exists: {target}")
        return 1
    findings = [Finding.from_dict(fd) for fd in report.get("findings") or [] if isinstance(fd, dict)]
    if not findings:
        print("No findings in this session.")
        return 0

    try:
        vulnerable, remediation, notes = choose_revisions(report, target, fixed)
    except WorkspaceError as exc:
        print(f"Error: {exc}")
        return 1
    scan_id = report.get("scan_id") or report_path.stem
    print(f"\n{'='*60}")
    print("  SECURITY REGRESSION TESTS")
    print(f"  Session:    {scan_id}")
    print(f"  Target:     {target}")
    print(f"  Vulnerable: {vulnerable.describe()}")
    print(f"  Remediated: {remediation.describe() if remediation else 'none (validation will stop at the vulnerable revision)'}")
    for note in notes:
        print(f"  Note:       {note}")
    print(f"{'='*60}\n")

    from openhack.agents.llm import LLMClient

    session = Session(target_dir=str(target), on_trace=_on_trace, parent_session_id=scan_id)
    llm = LLMClient(
        model=settings.regression_test_model_id, provider=settings.llm_provider,
        temperature=0.0, max_tokens=8192, prompt_cache_key=session.id,
    )
    options = PipelineOptions.from_settings(
        settings, str(target), vulnerable=vulnerable, remediation=remediation, report_id=scan_id,
    )
    pipeline = RegressionTestPipeline(llm, session, target, options)
    results = await pipeline.run(findings)

    by_id = {f.id: f for f in findings}
    for entry in report.get("findings") or []:
        if isinstance(entry, dict) and entry.get("id") in by_id:
            entry["regressionTest"] = by_id[entry["id"]].regression_test
    statuses: dict[str, int] = {}
    for result in results:
        statuses[result.status.value] = statuses.get(result.status.value, 0) + 1
    report["regression_tests"] = {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "session_id": session.id,
        "vulnerable": vulnerable.describe(),
        "remediated": remediation.describe() if remediation else None,
        "statuses": statuses,
        "cost": pipeline.total_cost,
    }
    _write_report_atomically(report_path, report)

    print(f"\n{'='*60}")
    print("  RESULTS")
    print(f"{'='*60}")
    for i, finding in enumerate(findings, 1):
        print(f"  {i}. [{finding.severity.upper()}] {finding.category} — {finding.file_path}")
        for line in format_regression_status(finding):
            print(f"     {line}")
        print()
    print(f"  Cost:   ${pipeline.total_cost:.4f}")
    print(f"  Report: {report_path}\n")
    # Non-zero only when an eligible finding ended without a validated test;
    # unsupported classes and evidence-free findings are not failures.
    not_applicable = {"unsupported_vulnerability", "missing_evidence"}
    failed = [r for r in results if r.status.value not in not_applicable | {"validated"}]
    return 2 if failed else 0
