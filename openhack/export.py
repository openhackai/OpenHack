"""Render and write findings as SARIF, Markdown, or JSON.

Pure module: no TUI or prompt_toolkit imports, so it can be used from the
interactive agent and from tests alike.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable, Optional, Union

from openhack import __version__ as OPENHACK_VERSION
from openhack.agents.eventlog import redact
from openhack.agents.session import Finding
from openhack.categories import CATEGORY_SEVERITY, normalize_category

FORMATS = ("sarif", "md", "json")

_ALIASES = {"sarif": "sarif", "md": "md", "markdown": "md", "json": "json"}
_EXTENSIONS = {".sarif": "sarif", ".json": "json", ".md": "md", ".markdown": "md"}

_LEVEL = {"critical": "error", "high": "error", "medium": "warning", "low": "note", "info": "note"}
_SCORE = {"critical": "9.5", "high": "8.0", "medium": "5.5", "low": "3.0", "info": "1.0"}
_SEVERITY_ORDER = ("critical", "high", "medium", "low", "info")

CWE_BY_CATEGORY: dict[str, int] = {
    "SQL Injection": 89,
    "Command Injection": 78,
    "RCE": 94,
    "XSS": 79,
    "SSRF": 918,
    "Open Redirect": 601,
    "Path Traversal": 22,
    "IDOR": 639,
    "Authentication Bypass": 287,
    "Authorization Bypass": 285,
    "CSRF": 352,
    "Data Exposure": 200,
    "Information Disclosure": 200,
    "Hardcoded Secret": 798,
    "Security Misconfiguration": 16,
    "Missing RLS": 284,
    "RPC Function Abuse": 284,
    "Storage Misconfiguration": 732,
    "Mass Assignment": 915,
    "Business Logic Flaw": 840,
    "Denial of Service": 400,
}

_LOCATION_RE = re.compile(r"^(?P<path>.+?):(?P<line>\d+)(?::(?P<col>\d+))?$")
_SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
_INFORMATION_URI = "https://openhack.com"
_SRCROOT = "%SRCROOT%"
_FINGERPRINT_KEY = "openhackFingerprint/v1"


class ExportError(ValueError):
    """Raised when an export format, destination, or write is invalid."""


def normalize_format(raw: str) -> str:
    key = (raw or "").strip().lower()
    if key not in _ALIASES:
        raise ExportError(
            f"unknown export format {raw!r} (use sarif, md, or json)"
        )
    return _ALIASES[key]


def infer_format_from_path(path: Union[str, Path]) -> Optional[str]:
    return _EXTENSIONS.get(Path(path).suffix.lower())


def default_filename(fmt: str) -> str:
    return f"openhack-findings.{normalize_format(fmt)}"


def findings_to_sarif(
    findings: Iterable[Finding],
    *,
    target: Union[str, Path, None] = None,
) -> dict:
    target_path = _target_path(target)
    rules: list[dict] = []
    rule_index: dict[str, int] = {}
    results: list[dict] = []
    seen_fingerprints: set[str] = set()

    for finding in findings:
        category = normalize_category(finding.category)
        if category not in rule_index:
            rule_index[category] = len(rules)
            rules.append(_sarif_rule(category))
        severity = _severity(finding.severity)
        uri, relative, line, column = _location(finding, target_path)

        region: dict = {"startLine": line or 1}
        if column:
            region["startColumn"] = column
        if finding.code_snippet:
            region["snippet"] = {"text": _scrub(finding.code_snippet)}

        artifact: dict = {"uri": uri}
        if relative:
            artifact["uriBaseId"] = _SRCROOT

        fingerprint = _unique_fingerprint(finding, seen_fingerprints)
        properties: dict = {
            "openhackSeverity": severity,
            "security-severity": _security_severity(finding, severity),
            "confidence": finding.confidence,
            "validated": finding.validated,
        }
        if finding.cvss_score is not None:
            properties["cvssScore"] = finding.cvss_score

        results.append({
            "ruleId": rules[rule_index[category]]["id"],
            "ruleIndex": rule_index[category],
            "level": _LEVEL[severity],
            "message": {"text": _message(finding, category)},
            "locations": [{
                "physicalLocation": {
                    "artifactLocation": artifact,
                    "region": region,
                },
            }],
            "partialFingerprints": {_FINGERPRINT_KEY: fingerprint},
            "properties": properties,
        })

    run: dict = {
        "tool": {
            "driver": {
                "name": "OpenHack",
                "version": OPENHACK_VERSION,
                "informationUri": _INFORMATION_URI,
                "rules": rules,
            },
        },
        "columnKind": "utf16CodeUnits",
        "results": results,
    }
    if target_path is not None:
        run["originalUriBaseIds"] = {
            _SRCROOT: {"uri": target_path.as_uri() + "/"},
        }
    return {"$schema": _SARIF_SCHEMA, "version": "2.1.0", "runs": [run]}


def findings_to_markdown(
    findings: Iterable[Finding],
    *,
    target: Union[str, Path, None] = None,
    generated_at: Optional[str] = None,
) -> str:
    items = list(findings)
    target_path = _target_path(target)
    stamp = generated_at or _now()

    lines = [
        "# OpenHack security findings",
        "",
        f"- **Target:** `{target_path or 'unknown'}`",
        f"- **Generated:** {stamp}",
        f"- **Findings:** {len(items)}",
        "",
        "## Summary",
        "",
        "| Severity | Count |",
        "| --- | ---: |",
    ]
    counts = {name: 0 for name in _SEVERITY_ORDER}
    for finding in items:
        counts[_severity(finding.severity)] += 1
    lines += [f"| {name} | {counts[name]} |" for name in _SEVERITY_ORDER]
    lines += ["", "## Findings", ""]

    if not items:
        lines += ["No findings.", ""]

    for index, finding in enumerate(items, 1):
        category = normalize_category(finding.category)
        severity = _severity(finding.severity)
        cwe = CWE_BY_CATEGORY.get(category)
        uri, _relative, line, _column = _location(finding, target_path)
        location = f"{uri}:{line}" if line else uri

        lines += [f"### {index}. {finding.title or category}", ""]
        lines.append(f"- **Severity:** {severity}")
        category_line = f"- **Category:** {category}"
        if cwe is not None:
            category_line += f" (CWE-{cwe})"
        lines.append(category_line)
        lines.append(f"- **Location:** `{location}`")
        if finding.cvss_score is not None:
            lines.append(f"- **CVSS:** {finding.cvss_score:g}")
        lines.append(f"- **Confidence:** {finding.confidence}")
        lines.append(f"- **Validated:** {'yes' if finding.validated else 'no'}")
        lines.append("")

        if finding.description:
            lines += [_scrub(finding.description), ""]
        if finding.code_snippet:
            snippet = _scrub(finding.code_snippet)
            lines += ["**Vulnerable code**", "", _fence(snippet), snippet, _fence(snippet), ""]
        if finding.fix:
            lines += ["**Recommended fix**", "", _scrub(finding.fix), ""]

    return "\n".join(lines).rstrip() + "\n"


def findings_to_json(
    findings: Iterable[Finding],
    *,
    target: Union[str, Path, None] = None,
    generated_at: Optional[str] = None,
) -> str:
    items = list(findings)
    target_path = _target_path(target)
    payload = {
        "tool": "openhack",
        "version": OPENHACK_VERSION,
        "generatedAt": generated_at or _now(),
        "target": str(target_path) if target_path is not None else None,
        "count": len(items),
        "findings": [redact(finding.to_dict()) for finding in items],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def render(
    findings: Iterable[Finding],
    fmt: str,
    *,
    target: Union[str, Path, None] = None,
) -> str:
    fmt = normalize_format(fmt)
    items = list(findings)
    if fmt == "sarif":
        return json.dumps(findings_to_sarif(items, target=target), indent=2, ensure_ascii=False) + "\n"
    if fmt == "md":
        return findings_to_markdown(items, target=target)
    return findings_to_json(items, target=target)


def write_export(
    findings: Iterable[Finding],
    fmt: str,
    dest: Union[str, Path],
    *,
    target: Union[str, Path, None] = None,
) -> Path:
    text = render(findings, fmt, target=target)
    dest_path = Path(dest)
    if dest_path.is_dir():
        raise ExportError(f"{dest_path} is a directory")
    parent = dest_path.parent
    if not parent.is_dir():
        raise ExportError(f"directory does not exist: {parent}")

    fd, tmp_name = tempfile.mkstemp(dir=parent, prefix=f".{dest_path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        _restrict_permissions(tmp_path)
        os.replace(tmp_path, dest_path)
    except OSError as exc:
        _unlink_quietly(tmp_path)
        raise ExportError(f"could not write {dest_path}: {exc}") from exc
    except BaseException:
        _unlink_quietly(tmp_path)
        raise
    return dest_path


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _target_path(target: Union[str, Path, None]) -> Optional[Path]:
    if target is None or not str(target).strip():
        return None
    return Path(target).expanduser().absolute()


def _severity(raw: Optional[str]) -> str:
    key = (raw or "").strip().lower()
    return key if key in _LEVEL else "medium"


def _scrub(text: str) -> str:
    return str(redact(text))


def _security_severity(finding: Finding, severity: str) -> str:
    if finding.cvss_score is not None:
        return f"{float(finding.cvss_score):.1f}"
    return _SCORE[severity]


def _message(finding: Finding, category: str) -> str:
    title = finding.title or category
    text = f"{title}: {finding.description}" if finding.description else title
    return _scrub(text)


def _sarif_rule(category: str) -> dict:
    severity = _severity(CATEGORY_SEVERITY.get(category, "medium"))
    slug = "-".join(category.lower().split())
    tags = ["security"]
    cwe = CWE_BY_CATEGORY.get(category)
    if cwe is not None:
        tags.append(f"external/cwe/cwe-{cwe}")
    return {
        "id": f"openhack/{slug}",
        "name": _rule_name(category),
        "shortDescription": {"text": category},
        "defaultConfiguration": {"level": _LEVEL[severity]},
        "properties": {
            "tags": tags,
            "security-severity": _SCORE[severity],
        },
    }


def _rule_name(category: str) -> str:
    return "".join(
        word if word.isupper() else word[:1].upper() + word[1:]
        for word in category.split()
    )


def _split_location(file_path: str) -> tuple[str, Optional[int], Optional[int]]:
    match = _LOCATION_RE.match(file_path.strip())
    if match is None:
        return file_path.strip(), None, None
    line = int(match.group("line"))
    column = int(match.group("col")) if match.group("col") else None
    return match.group("path"), line, column


def _artifact_uri(path_text: str, target: Optional[Path]) -> tuple[str, bool]:
    if not path_text:
        return "unknown", True
    path = Path(path_text)
    if not path.is_absolute():
        return PurePosixPath(*path.parts).as_posix(), True
    if target is not None:
        try:
            relative = path.relative_to(target)
        except ValueError:
            pass
        else:
            return PurePosixPath(*relative.parts).as_posix() or ".", True
    return path.as_uri(), False


def _location(finding: Finding, target: Optional[Path]) -> tuple[str, bool, Optional[int], Optional[int]]:
    path_text, parsed_line, parsed_column = _split_location(finding.file_path or "")
    line = finding.line_number or parsed_line
    uri, relative = _artifact_uri(path_text, target)
    return uri, relative, line, parsed_column


def _unique_fingerprint(finding: Finding, seen: set[str]) -> str:
    base = hashlib.sha256(f"{finding.fingerprint()}::{finding.title}".encode()).hexdigest()[:32]
    fingerprint = base
    if fingerprint in seen:
        fingerprint = hashlib.sha256(f"{base}::{finding.id}".encode()).hexdigest()[:32]
    seen.add(fingerprint)
    return fingerprint


def _fence(text: str) -> str:
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def _restrict_permissions(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
