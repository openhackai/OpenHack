"""
Run ONLY the feature deep dive on a target repo.
Skips category hunters — runs recon → feature extraction → feature hunters → validation.

Usage: uv run python run_feature_hunt.py /path/to/repo
"""

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

os.environ.setdefault("LLM_PROVIDER", "openhack")

from openhack.agents.coordinator import CoordinatorAgent
from openhack.agents.recon import ReconAgent
from openhack.agents.feature_hunter import FeatureHunterAgent
from openhack.agents.validator_swarm import ValidatorSwarmAgent
from openhack.agents.hunter_swarm import HunterSwarmAgent
from openhack.agents.llm import LLMClient
from openhack.agents.session import Session, Finding, SessionStatus, TraceEntry
from openhack.tools.registry import ToolRegistry
from openhack.tools.coverage import discover_attack_surface
from openhack.framework_detection import detect_frameworks
from openhack.deterministic_recon import run_deterministic_recon
from openhack.framework_classifier import classify_frameworks
from openhack.entry_points import detect_entry_points
from openhack.scan_session import ScanSession
from openhack.quality import run_quality_gates
from openhack.categories import normalize_category, normalize_severity
from openhack.config import reload_settings, resolve_validator_model, settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("feature_hunt")


def on_trace(entry: TraceEntry):
    agent = entry.agent or "?"
    event = entry.event_type or ""
    content = str(entry.content or "")
    if event == "tool_call":
        print(f"  [{agent}] tool: {entry.tool_name}")
    elif event in ("finding", "finding_added"):
        print(f"  [{agent}] FINDING: {content}")
    elif event in ("status", "step_start", "step_complete"):
        snippet = (content[:160] + "...") if len(content) > 160 else content
        print(f"  [{agent}] {event}: {snippet}")


async def run(target_dir: str, resume_session_id: str = None):
    reload_settings()
    target_path = Path(target_dir)

    print(f"\n{'='*60}")
    print(f"  FEATURE DEEP DIVE — {target_dir}")
    print(f"  Provider: {settings.llm_provider}")
    print(f"{'='*60}\n")

    session = Session(target_dir=target_dir, on_trace=on_trace)
    tools = ToolRegistry(target_dir=target_path)
    total_cost = 0.0
    total_tokens = 0

    # ── Step 1: Framework Classification + Entry Points ─────────
    if resume_session_id:
        # Resume from existing session
        scan_session = ScanSession.load(resume_session_id)
        if not scan_session:
            print(f"  Session {resume_session_id} not found")
            return
        classifications = scan_session.classifications
        entry_points = scan_session.entry_points
        unscanned = scan_session.get_unscanned_files()
        print(f"[1/5] Resuming session {resume_session_id}")
        print(f"  {len(entry_points)} total endpoints, {scan_session.scanned_count} already scanned")
        print(f"  {scan_session.unscanned_count} endpoints remaining ({len(unscanned)} files)")
        print(f"  Previous findings: {len(scan_session.findings)}")
        if scan_session.unscanned_count == 0:
            print(f"\n  All endpoints already scanned! Nothing to do.")
            print(f"  Use a new scan to re-check, or pick a different repo.")
            scan_session.print_summary()
            return
    else:
        print("[1/5] Classifying frameworks...")
        classifications = classify_frameworks(tools.fs_tools)
        for c in classifications:
            print(f"  {c['root']} → {c['language']} [{', '.join(c['frameworks'])}]")

        print("\n[2/5] Detecting entry points...")
        entry_points = detect_entry_points(tools.fs_tools, classifications)
        print(f"  {len(entry_points)} entry points found")

        # Create new scan session
        import uuid as _uuid
        scan_session = ScanSession(str(_uuid.uuid4())[:8], target_dir)
        scan_session.classifications = classifications
        scan_session.entry_points = entry_points
        scan_session.save()
        print(f"  Session: {scan_session.session_id}")

    # ── Step 2: Deterministic Recon ──────────────────────────────
    print("\n[3/5] Running deterministic reconnaissance...")
    recon_result = run_deterministic_recon(tools)
    detected_frameworks = recon_result.get("frameworks", [])
    features_detected = list(recon_result.get("features", {}).keys())
    print(f"  Features: {features_detected}")
    print(f"  Recon complete: $0.00 (deterministic)")

    # ── Step 2: Extract features ───────────────────────────────
    print("\n[3.5/5] Setting up researcher agents...")
    coordinator = CoordinatorAgent(
        LLMClient(provider=settings.llm_provider, temperature=0.0, max_tokens=8192),
        tools, session,
    )
    coordinator.context = {"recon": recon_result}

    # Researcher mode: hardcoded researchers + manager-written app-specific ones
    features = []  # empty = researcher mode
    print("  Researcher mode — hardcoded + manager-written researchers")

    # ── Step 3: Feature deep dive ──────────────────────────────
    print(f"\n[4/5] Running deep dive (hardcoded + manager-written researchers)...")
    context = {
        "recon": recon_result,
        "detected_frameworks": detected_frameworks,
    }

    feature_result = await coordinator._run_feature_deep_dive(features, context)

    feature_findings = feature_result.get("findings", [])
    feature_cost = feature_result["total_cost"]
    feature_tokens = feature_result["total_tokens"]
    total_cost += feature_cost
    total_tokens += feature_tokens

    print(f"  Feature hunt complete: {len(feature_findings)} raw findings, "
          f"${feature_cost:.4f}, {feature_tokens:,} tokens")

    if not feature_findings:
        print("\n  No findings from feature hunt.")
        print(f"\n  Total cost: ${total_cost:.4f}")
        return

    # ── Step 5: LLM Validation ──────────────────────────────────
    print(f"\n[5/5] Validating {len(feature_findings)} findings...")
    feature_hunter_model = (
        getattr(settings, "feature_hunter_model_id", None)
        or getattr(settings, "hunter_model_id", None)
        or coordinator.llm.model
    )
    validator_model = resolve_validator_model(
        hunter_model=feature_hunter_model,
        requested_validator_model=settings.validator_model_id,
        provider=settings.llm_provider,
    )
    validator_llm = LLMClient(
        model=validator_model,
        provider=settings.llm_provider,
        temperature=0.0,
        max_tokens=8192,
    )
    validator_context = {
        "hunter": {"findings": feature_findings},
        "recon": recon_result,
    }
    validator_swarm = ValidatorSwarmAgent(
        validator_llm, tools, session, hunter_model=feature_hunter_model
    )
    validator_result = await validator_swarm.run(
        "Validate each potential vulnerability.", context=validator_context,
    )

    validated = validator_result.get("validated_findings", [])
    false_positives = validator_result.get("false_positives", [])
    unreviewed = validator_result.get("unreviewed", [])
    validator_cost = validator_swarm.total_cost
    total_cost += validator_cost
    total_tokens += validator_swarm.total_tokens

    print(f"  Validation complete: {len(validated)} confirmed, "
          f"{len(false_positives)} false positives, "
          f"{len(unreviewed)} unreviewed, ${validator_cost:.4f}")

    # Deduplicate and cap
    validated = CoordinatorAgent._deduplicate_validated(validated, feature_findings)
    validated = CoordinatorAgent._cap_findings_per_file(validated, feature_findings)

    # ── Results ────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  RESULTS")
    print(f"{'='*60}")

    if not validated:
        print("  No vulnerabilities confirmed.")
    else:
        print(f"  {len(validated)} vulnerability(ies) confirmed:\n")
        for i, v in enumerate(validated, 1):
            idx = v.get("original_index", 0)
            if 0 <= idx < len(feature_findings):
                f = feature_findings[idx]
                sev = f.get("severity", "?").upper()
                cat = f.get("category", "?")
                fp = f.get("file_path", "?")
                desc = (f.get("description", "")[:200])
                print(f"  {i}. [{sev}] {cat} — {fp}")
                print(f"     {desc}")
                if v.get("poc"):
                    print(f"     PoC: {v['poc'][:100]}...")
                print()

    elapsed = time.time() - session.created_at
    m, s = divmod(int(elapsed), 60)
    print(f"  Cost:     ${total_cost:.4f}")
    print(f"  Tokens:   {total_tokens:,}")
    print(f"  Duration: {m}m {s:02d}s")

    # Save report
    report_dir = Path.home() / ".openhack" / "scans"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"feature-hunt-{session.id}.json"
    report = {
        "version": 1,
        "scan_id": session.id,
        "scan_type": "feature_deep_dive",
        "target_dir": target_dir,
        "provider": settings.llm_provider,
        "features_analyzed": [f.get("name") for f in features],
        "duration_seconds": round(elapsed, 2),
        "cost": total_cost,
        "raw_findings": len(feature_result.get("findings", [])),
        "validated_findings": len(validated),
        "findings": [
            {
                **feature_findings[v["original_index"]],
                "validation": v,
            }
            for v in validated
            if 0 <= v.get("original_index", -1) < len(feature_findings)
        ],
    }
    with open(report_path, "w") as fp:
        json.dump(report, fp, indent=2, default=str)
    print(f"\n  Report: {report_path}")

    # ── Update scan session ──────────────────────────────────
    # Mark files that researchers analyzed as scanned
    files_analyzed = feature_result.get("files_analyzed", [])
    for f in files_analyzed:
        scan_session.mark_scanned(f)

    # Mark files with findings
    for f in feature_findings:
        fp = f.get("file_path", "")
        if fp:
            scan_session.mark_finding(fp)

    scan_session.findings = [
        {
            "severity": feature_findings[v["original_index"]].get("severity", "?"),
            "category": feature_findings[v["original_index"]].get("category", "?"),
            "file_path": feature_findings[v["original_index"]].get("file_path", "?"),
            "description": feature_findings[v["original_index"]].get("description", "")[:200],
        }
        for v in validated
        if 0 <= v.get("original_index", -1) < len(feature_findings)
    ]
    scan_session.total_cost = total_cost
    scan_session.save()
    scan_session.print_summary()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="OpenHack Scanner — Feature Deep Dive")
    parser.add_argument("target", nargs="?", default=".", help="Target directory to scan")
    parser.add_argument("--list-sessions", action="store_true", help="List all saved scan sessions")
    parser.add_argument("--list-entry-points", metavar="SESSION_ID", help="List entry points for a session")
    parser.add_argument("--resume", metavar="SESSION_ID", help="Resume a previous scan session")

    args = parser.parse_args()

    if args.list_sessions:
        sessions = ScanSession.list_sessions()
        print(f"\nSaved sessions: {len(sessions)}")
        for s in sessions:
            stats = s.get("stats", {})
            print(f"  {s['session_id']} — {s['target_dir']} — "
                  f"{stats.get('total', 0)} endpoints, "
                  f"{stats.get('coverage_pct', 0)}% covered, "
                  f"{stats.get('findings_count', 0)} findings")
        sys.exit(0)

    if args.list_entry_points:
        session = ScanSession.load(args.list_entry_points)
        if not session:
            print(f"Session {args.list_entry_points} not found")
            sys.exit(1)
        session.print_entry_points(show_all=True)
        sys.exit(0)

    if args.resume:
        session = ScanSession.load(args.resume)
        if not session:
            print(f"Session {args.resume} not found")
            sys.exit(1)
        asyncio.run(run(session.target_dir, resume_session_id=args.resume))
    else:
        asyncio.run(run(args.target))
