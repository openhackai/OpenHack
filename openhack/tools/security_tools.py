"""
High-signal security scanners that give the agent a deterministic head start.

The philosophy (shared with the vuln scanner) is a funnel: run wide, cheap,
deterministic detectors first, then let the agent triage the candidates with
context. These tools do the "wide + cheap" step:

  * sca_scan     — supply-chain / dependency vulnerabilities via osv-scanner
  * secret_scan  — leaked-credential candidates via high-signal patterns

Both degrade gracefully: if an external engine (osv-scanner) is missing, they
say so and fall back to whatever they can do natively, rather than erroring.
"""

import json
import re
import subprocess
from pathlib import Path
from shutil import which
from typing import Optional


# ------------------------------------------------------------------ secrets

# High-signal patterns only. These are tuned for precision, not recall: each is
# something that is almost always a real credential when it appears verbatim.
# The agent is expected to triage the candidates (dead test keys, examples,
# rotated values) — this step just narrows millions of lines to a short list.
_SECRET_PATTERNS: list[tuple[str, str]] = [
    ("aws_access_key_id", r"\b(?:A3T[A-Z0-9]|AKIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|ASIA)[A-Z0-9]{16}\b"),
    ("aws_secret_access_key", r"(?i)aws_secret_access_key\s*[=:]\s*['\"]?([A-Za-z0-9/+=]{40})['\"]?"),
    ("private_key", r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"),
    ("github_token", r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}\b"),
    ("github_fine_grained", r"\bgithub_pat_[A-Za-z0-9_]{82}\b"),
    ("slack_token", r"\bxox[baprs]-[A-Za-z0-9-]{10,72}\b"),
    ("stripe_secret_key", r"\b(?:sk|rk)_live_[A-Za-z0-9]{20,60}\b"),
    ("google_api_key", r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    ("openai_key", r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}T3BlbkFJ[A-Za-z0-9_\-]{20,}\b"),
    ("anthropic_key", r"\bsk-ant-[A-Za-z0-9-]{90,}\b"),
    ("jwt", r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),
    ("generic_secret_assignment",
     r"(?i)(?:api[_-]?key|secret|passwd|password|token|access[_-]?token)\s*[=:]\s*['\"]([^'\"\s]{16,})['\"]"),
    ("slack_webhook", r"https://hooks\.slack\.com/services/T[A-Za-z0-9]+/B[A-Za-z0-9]+/[A-Za-z0-9]+"),
    ("private_key_url", r"postgres(?:ql)?://[^:\s]+:[^@\s]+@[^\s]+"),
]

_SECRET_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".next", ".nuxt", ".output", "vendor", "target", "coverage", ".mypy_cache",
    ".pytest_cache", ".tox",
}

_SECRET_SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".svg", ".pdf", ".zip",
    ".gz", ".tar", ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".mp3", ".lock",
    ".min.js", ".map",
}


class SecurityTools:
    """Dependency (SCA) and secret scanning tools."""

    MAX_FILE_BYTES = 2_000_000
    MAX_SECRET_HITS = 200

    def __init__(self, workdir: Optional[Path] = None):
        self.workdir = Path(workdir).resolve() if workdir else Path.cwd()

    # ------------------------------------------------------------------ paths

    def _resolve(self, path: Optional[str]) -> Path:
        if not path:
            return self.workdir
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.workdir / candidate
        return candidate.resolve()

    # -------------------------------------------------------------------- SCA

    def sca_scan(self, path: Optional[str] = None) -> dict:
        """Scan project dependencies for known vulnerabilities (supply chain).

        Uses osv-scanner against the OSV database. Reports the affected package,
        version, vulnerability IDs and severity so the agent can prioritise.
        """
        target = self._resolve(path)
        if not target.exists():
            return {"error": f"Path does not exist: {target}"}

        if which("osv-scanner") is None:
            return self._sca_fallback(target)

        try:
            proc = subprocess.run(
                ["osv-scanner", "scan", "source", "--format", "json",
                 "--recursive", "--allow-no-lockfiles", str(target)],
                capture_output=True, text=True, timeout=600,
            )
        except subprocess.TimeoutExpired:
            return {"engine": "osv-scanner", "error": "osv-scanner timed out after 600s"}
        except Exception as e:  # pragma: no cover - defensive
            return {"engine": "osv-scanner", "error": f"osv-scanner failed: {e}"}

        # osv-scanner exits non-zero (1) when vulns are found — that's success.
        raw = proc.stdout.strip()
        if not raw:
            return {
                "engine": "osv-scanner",
                "vulnerable_packages": 0,
                "findings": [],
                "note": proc.stderr.strip()[:500] or "No dependencies or lockfiles found.",
            }
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {"engine": "osv-scanner", "error": "Could not parse osv-scanner output",
                    "raw": raw[:1000]}

        return self._normalize_osv(data, target)

    def _normalize_osv(self, data: dict, target: Path) -> dict:
        findings = []
        for res in data.get("results", []):
            source = res.get("source", {}).get("path", "")
            try:
                source = str(Path(source).relative_to(target))
            except (ValueError, TypeError):
                pass
            for pkg in res.get("packages", []):
                info = pkg.get("package", {})
                for vuln in pkg.get("vulnerabilities", []):
                    sev = ""
                    for s in vuln.get("severity", []) or []:
                        sev = s.get("score", sev)
                    aliases = vuln.get("aliases", []) or []
                    ident = vuln.get("id", "")
                    findings.append({
                        "package": info.get("name", ""),
                        "ecosystem": info.get("ecosystem", ""),
                        "version": info.get("version", ""),
                        "id": ident,
                        "aliases": aliases,
                        "summary": (vuln.get("summary") or "")[:300],
                        "severity": sev,
                        "source": source,
                    })
        return {
            "engine": "osv-scanner",
            "vulnerable_packages": len({f["package"] for f in findings}),
            "count": len(findings),
            "findings": findings[:200],
            "truncated": len(findings) > 200,
        }

    def _sca_fallback(self, target: Path) -> dict:
        """No osv-scanner: at least inventory the lockfiles present."""
        lockfiles = [
            "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "requirements.txt",
            "poetry.lock", "Pipfile.lock", "go.mod", "go.sum", "Cargo.lock",
            "Gemfile.lock", "composer.lock", "pom.xml", "build.gradle",
        ]
        found = []
        if target.is_dir():
            for name in lockfiles:
                for match in target.rglob(name):
                    if not any(part in _SECRET_SKIP_DIRS for part in match.parts):
                        found.append(str(match.relative_to(target)))
        return {
            "engine": "none",
            "note": (
                "osv-scanner is not installed, so I could not check dependencies "
                "against the OSV vulnerability database. Install it "
                "(`brew install osv-scanner`) for real SCA. Lockfiles detected below."
            ),
            "lockfiles": sorted(set(found))[:100],
        }

    # ---------------------------------------------------------------- secrets

    def secret_scan(self, path: Optional[str] = None, max_hits: Optional[int] = None) -> dict:
        """Scan files for leaked credential candidates (high-signal patterns).

        Returns candidate hits with file, line and a redacted preview. These are
        candidates, not confirmed leaks — triage them for test/example/rotated
        values before reporting.
        """
        target = self._resolve(path)
        if not target.exists():
            return {"error": f"Path does not exist: {target}"}

        cap = min(int(max_hits), self.MAX_SECRET_HITS) if max_hits else self.MAX_SECRET_HITS
        compiled = [(name, re.compile(pat)) for name, pat in _SECRET_PATTERNS]

        files = [target] if target.is_file() else self._iter_files(target)
        hits = []
        scanned = 0
        for fpath in files:
            if len(hits) >= cap:
                break
            try:
                if fpath.stat().st_size > self.MAX_FILE_BYTES:
                    continue
                text = fpath.read_text(encoding="utf-8", errors="ignore")
            except (OSError, ValueError):
                continue
            scanned += 1
            for lineno, line in enumerate(text.splitlines(), 1):
                if len(line) > 4000:
                    continue
                for name, rx in compiled:
                    if rx.search(line):
                        hits.append({
                            "type": name,
                            "file": self._rel(fpath, target),
                            "line": lineno,
                            "preview": self._redact(line.strip()),
                        })
                        break
                if len(hits) >= cap:
                    break

        return {
            "engine": "openhack-secrets",
            "files_scanned": scanned,
            "count": len(hits),
            "candidates": hits,
            "truncated": len(hits) >= cap,
            "note": (
                "These are high-signal candidates, not confirmed leaks. Open each "
                "location, judge whether it's a live credential vs a test/example/"
                "placeholder, and check git history for rotation."
            ),
        }

    def _iter_files(self, root: Path):
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            if any(part in _SECRET_SKIP_DIRS for part in p.parts):
                continue
            if p.suffix.lower() in _SECRET_SKIP_SUFFIXES:
                continue
            if p.name.endswith(".min.js") or p.name.endswith(".map"):
                continue
            yield p

    def _rel(self, p: Path, root: Path) -> str:
        try:
            return str(p.relative_to(root))
        except ValueError:
            return str(p)

    def _redact(self, line: str) -> str:
        """Show enough to locate the secret, but mask the sensitive middle."""
        line = line[:200]

        def _mask(m: re.Match) -> str:
            s = m.group(0)
            if len(s) <= 10:
                return s[:2] + "***"
            return s[:4] + "***" + s[-2:]

        for _, pat in _SECRET_PATTERNS:
            line = re.sub(pat, _mask, line)
        return line

    # -------------------------------------------------------------- tool specs

    def get_tool_definitions(self) -> list[dict]:
        return [
            {
                "name": "sca_scan",
                "description": (
                    "Supply-chain / dependency scan: check the project's dependency "
                    "lockfiles against the OSV vulnerability database (via osv-scanner) "
                    "and return vulnerable packages with versions, advisory IDs and "
                    "severity. Run this early for a fast head start on known-CVE risk."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Directory or lockfile to scan (defaults to the session root).",
                        },
                    },
                },
            },
            {
                "name": "secret_scan",
                "description": (
                    "Scan files for leaked-credential candidates (AWS/GCP keys, private "
                    "keys, GitHub/Slack/Stripe tokens, JWTs, DB URLs, generic secret "
                    "assignments) using high-signal patterns. Returns file:line "
                    "candidates to triage. Run early for a head start on exposed secrets."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Directory or file to scan (defaults to the session root).",
                        },
                        "max_hits": {
                            "type": "integer",
                            "description": "Maximum candidates to return (default 200).",
                        },
                    },
                },
            },
        ]

    def execute_tool(self, name: str, arguments: dict) -> dict:
        import inspect

        tools = {
            "sca_scan": self.sca_scan,
            "secret_scan": self.secret_scan,
        }
        if name not in tools:
            return {"error": f"Unknown tool: {name}"}
        func = tools[name]
        valid = set(inspect.signature(func).parameters.keys())
        filtered = {k: v for k, v in arguments.items() if k in valid}
        return func(**filtered)
