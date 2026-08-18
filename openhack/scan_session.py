"""
Scan session — tracks which entry points have been analyzed across runs.

Provides resume capability: start a scan, stop partway, resume later from
where you left off. Each session has a unique ID and persists to disk.
"""

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SESSIONS_DIR = Path.home() / ".openhack" / "scans"
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


class ScanSession:
    """Tracks scan progress across entry points."""

    def __init__(self, session_id: str, target_dir: str):
        self.session_id = session_id
        self.target_dir = target_dir
        self.created_at = datetime.now().isoformat()
        self.updated_at = self.created_at
        self.classifications = []  # Framework classifications
        self.entry_points = []     # All detected entry points
        self.findings = []         # Findings from scanned entry points
        self.total_cost = 0.0
        self.attack_surface: Optional[dict] = None
        self.analyzed_files: list[str] = []
        self.zone_coverage: dict[str, dict] = {}  # zone_name -> {status, files_total, files_done}

    @property
    def scanned_count(self) -> int:
        return sum(1 for ep in self.entry_points if ep.get("status") != "unscanned")

    @property
    def unscanned_count(self) -> int:
        return sum(1 for ep in self.entry_points if ep.get("status") == "unscanned")

    @property
    def coverage_pct(self) -> float:
        total = len(self.entry_points)
        if total == 0:
            return 0.0
        return (self.scanned_count / total) * 100

    def mark_scanned(self, file_path: str, status: str = "scanned"):
        """Mark all entry points in a file as scanned."""
        for ep in self.entry_points:
            if ep.get("file") == file_path:
                ep["status"] = status
        self.updated_at = datetime.now().isoformat()

    def mark_finding(self, file_path: str):
        """Mark entry points in a file as having findings."""
        for ep in self.entry_points:
            if ep.get("file") == file_path:
                ep["status"] = "finding_found"
        self.updated_at = datetime.now().isoformat()

    def get_unscanned_files(self) -> list[str]:
        """Get list of unique files with unscanned entry points."""
        files = set()
        for ep in self.entry_points:
            if ep.get("status") == "unscanned":
                files.add(ep.get("file", ""))
        return sorted(files)

    def get_scanned_files(self) -> list[str]:
        """Get list of files that have been scanned."""
        files = set()
        for ep in self.entry_points:
            if ep.get("status") != "unscanned":
                files.add(ep.get("file", ""))
        return sorted(files)

    def mark_zone_complete(self, zone_name: str, files_analyzed: list[str], findings_count: int = 0):
        """Mark a zone as completed after a researcher finishes."""
        self.zone_coverage[zone_name] = {
            "status": "completed",
            "files_analyzed": files_analyzed,
            "files_done": len(files_analyzed),
            "findings_count": findings_count,
            "completed_at": datetime.now().isoformat(),
        }
        for f in files_analyzed:
            if f not in self.analyzed_files:
                self.analyzed_files.append(f)
        self.updated_at = datetime.now().isoformat()

    def get_completed_zones(self) -> set[str]:
        """Get names of zones that have been fully analyzed."""
        return {
            name for name, info in self.zone_coverage.items()
            if info.get("status") == "completed"
        }

    def get_analyzed_file_set(self) -> set[str]:
        """Get all files that have been analyzed across all zones."""
        return set(self.analyzed_files)

    def save(self):
        """Persist session to disk."""
        path = SESSIONS_DIR / f"{self.session_id}.json"
        data = {
            "session_id": self.session_id,
            "target_dir": self.target_dir,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "classifications": self.classifications,
            "entry_points": self.entry_points,
            "findings": self.findings,
            "total_cost": self.total_cost,
            "attack_surface": self.attack_surface,
            "analyzed_files": self.analyzed_files,
            "zone_coverage": self.zone_coverage,
            "stats": {
                "total": len(self.entry_points),
                "scanned": self.scanned_count,
                "unscanned": self.unscanned_count,
                "coverage_pct": round(self.coverage_pct, 1),
                "findings_count": len(self.findings),
                "zones_completed": len(self.get_completed_zones()),
                "files_analyzed": len(self.analyzed_files),
            },
        }
        path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        logger.debug(f"Session {self.session_id} saved: {self.scanned_count}/{len(self.entry_points)} scanned")

    @classmethod
    def load(cls, session_id: str) -> Optional["ScanSession"]:
        """Load a session from disk."""
        path = SESSIONS_DIR / f"{session_id}.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        session = cls(data["session_id"], data["target_dir"])
        session.created_at = data.get("created_at", "")
        session.updated_at = data.get("updated_at", "")
        session.classifications = data.get("classifications", [])
        session.entry_points = data.get("entry_points", [])
        session.findings = data.get("findings", [])
        session.total_cost = data.get("total_cost", 0.0)
        session.attack_surface = data.get("attack_surface")
        session.analyzed_files = data.get("analyzed_files", [])
        session.zone_coverage = data.get("zone_coverage", {})
        return session

    @classmethod
    def list_sessions(cls, target_dir: Optional[str] = None) -> list[dict]:
        """List all saved sessions, optionally filtered by target_dir."""
        sessions = []
        for path in sorted(SESSIONS_DIR.glob("*.json"), reverse=True):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if target_dir and data.get("target_dir") != target_dir:
                    continue
                sessions.append({
                    "session_id": data["session_id"],
                    "target_dir": data.get("target_dir", "?"),
                    "created_at": data.get("created_at", "?"),
                    "stats": data.get("stats", {}),
                })
            except Exception:
                continue
        return sessions

    def print_summary(self):
        """Print a human-readable summary of the session."""
        print(f"\n{'='*60}")
        print(f"  Scan Session: {self.session_id}")
        print(f"  Target: {self.target_dir}")
        print(f"  Created: {self.created_at}")
        print(f"{'='*60}")

        # Framework summary
        if self.classifications:
            print(f"\n  Frameworks:")
            for c in self.classifications:
                print(f"    {c['root']} → {c['language']} [{', '.join(c['frameworks'])}]")

        # Entry point summary
        total = len(self.entry_points)
        scanned = self.scanned_count
        unscanned = self.unscanned_count
        with_findings = sum(1 for ep in self.entry_points if ep.get("status") == "finding_found")

        print(f"\n  Entry Points: {total}")
        print(f"    Scanned:    {scanned} ({self.coverage_pct:.1f}%)")
        print(f"    Unscanned:  {unscanned}")
        print(f"    w/ Findings: {with_findings}")

        # Zone coverage
        if self.zone_coverage:
            completed = self.get_completed_zones()
            print(f"\n  Zones: {len(completed)} completed")
            for name, info in self.zone_coverage.items():
                status = info.get("status", "?")
                done = info.get("files_done", 0)
                findings = info.get("findings_count", 0)
                icon = "[✓]" if status == "completed" else "[ ]"
                print(f"    {icon} {name} — {done} files, {findings} findings")

        if self.analyzed_files:
            print(f"\n  Files Analyzed: {len(self.analyzed_files)}")

        # Findings summary
        if self.findings:
            print(f"\n  Findings: {len(self.findings)}")
            for f in self.findings:
                sev = f.get("severity", "?").upper()
                cat = f.get("category", "?")
                fp = f.get("file_path", "?")
                print(f"    [{sev}] {cat} — {fp}")

        print(f"\n  Cost: ${self.total_cost:.4f}")
        print()

    def print_entry_points(self, show_all: bool = False):
        """Print all entry points with their scan status."""
        print(f"\n  {'Status':<15} {'Method':<10} {'Path':<50} {'File'}")
        print(f"  {'-'*100}")
        for ep in self.entry_points:
            status = ep.get("status", "unscanned")
            if not show_all and status != "unscanned":
                continue
            status_icon = {
                "unscanned": "[ ]",
                "scanned": "[✓]",
                "finding_found": "[!]",
                "clean": "[·]",
            }.get(status, "[?]")
            method = ep.get("method", "?")
            path = ep.get("path", "?")
            file = ep.get("file", "?")
            # Truncate long paths
            if len(path) > 48:
                path = path[:45] + "..."
            print(f"  {status_icon:<15} {method:<10} {path:<50} {file}")
