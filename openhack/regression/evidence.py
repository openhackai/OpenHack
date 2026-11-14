"""
Exploit evidence from sandbox verification, made safe to persist and reuse.

The sandbox verifier talks to a live application with real session tokens,
cookies and passwords. Only a redacted, bounded summary is kept on the
Finding: it feeds invariant extraction and the generator prompt, so it must
never be able to carry a credential into a generated test, a report, or an
LLM request. Generated test code gets a second, independent secret scan.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from openhack.agents.eventlog import redact

MAX_EVIDENCE_CHARS = 2000
MAX_POC_CHARS = 4000
MAX_BODY_CHARS = 1000

_SENSITIVE_HEADER = re.compile(
    r"(authorization|cookie|api[-_]?key|auth[-_]?token|csrf|xsrf|session|secret)",
    re.IGNORECASE,
)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}")
_COOKIE_LINE = re.compile(r"(?im)^(\s*(?:set-)?cookie\s*:\s*).+$")
_ASSIGNED_SECRET = re.compile(
    r"""(?ix)
    (["']?(?:password|passwd|secret|token|api[_-]?key|session(?:_?id)?|cookie)["']?
     \s*[:=]\s*)
    (["'])(?:(?!\2).){4,}\2
    """
)

# Credential shapes that must never appear in a generated test. These are
# deliberately specific: tests legitimately contain throwaway passwords such
# as "correct-horse", but a live JWT or cloud key is always a leak.
SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("JSON Web Token", _JWT),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Stripe live key", re.compile(r"\b[sr]k_live_[0-9A-Za-z]{16,}\b")),
    ("API secret key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("literal bearer token", re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/-]{24,}=*")),
    ("redacted evidence placeholder", re.compile(r"\[REDACTED[A-Z_]*\]")),
]


def scrub_text(text: str) -> str:
    """Remove credentials from free-form evidence text."""
    if not text:
        return ""
    cleaned = redact(str(text))
    cleaned = _JWT.sub("[REDACTED_JWT]", cleaned)
    cleaned = _COOKIE_LINE.sub(r"\1[REDACTED]", cleaned)
    cleaned = _ASSIGNED_SECRET.sub(r"\1\2[REDACTED]\2", cleaned)
    return cleaned


def _bounded(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _sanitize_body(body: Any) -> Optional[str]:
    if body is None or body == "":
        return None
    if isinstance(body, (dict, list)):
        return _bounded(scrub_text(json.dumps(redact(body), default=str)), MAX_BODY_CHARS)
    text = str(body)
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return _bounded(scrub_text(text), MAX_BODY_CHARS)
    return _bounded(scrub_text(json.dumps(redact(parsed), default=str)), MAX_BODY_CHARS)


def _sanitize_request(request: Any) -> Optional[dict]:
    if not isinstance(request, dict):
        return None
    path = str(request.get("path") or request.get("url") or "").strip()
    method = str(request.get("method") or "").strip().upper()
    if not path and not method:
        return None
    headers = request.get("headers")
    clean_headers: dict[str, str] = {}
    if isinstance(headers, dict):
        for key, value in list(headers.items())[:30]:
            name = str(key)[:100]
            clean_headers[name] = (
                "[REDACTED]" if _SENSITIVE_HEADER.search(name)
                else _bounded(scrub_text(str(value)), 200)
            )
    body = request.get("json_body", request.get("body"))
    return {
        "method": method[:10],
        "path": _bounded(scrub_text(path), 500),
        "headers": clean_headers,
        "body": _sanitize_body(body),
    }


def sanitize_exploit_evidence(result: Optional[dict]) -> Optional[dict]:
    """Build the persisted evidence record from a sandbox ``exploitable`` result.

    Returns None when the result carries nothing that identifies the exploit,
    so "verified but evidence-free" findings are treated as missing evidence
    instead of producing a guessed invariant.
    """
    if not isinstance(result, dict):
        return None
    request = _sanitize_request(result.get("exploit_request"))
    evidence = _bounded(scrub_text(str(result.get("evidence") or "")), MAX_EVIDENCE_CHARS)
    poc = _bounded(scrub_text(str(result.get("working_poc") or "")), MAX_POC_CHARS)
    if not request and not evidence.strip() and not poc.strip():
        return None
    try:
        attempts = int(result.get("attempts_made") or 0)
    except (TypeError, ValueError):
        attempts = 0
    return {
        "source": "sandbox",
        "exploit_request": request,
        "evidence": evidence,
        "working_poc": poc,
        "attempts_made": attempts,
        "confidence": str(result.get("confidence") or ""),
    }


def find_secrets(code: str) -> list[str]:
    """Return the names of credential patterns present in generated code."""
    found = []
    for name, pattern in SECRET_PATTERNS:
        if pattern.search(code or ""):
            found.append(name)
    return found
