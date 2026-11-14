"""
Security-invariant extraction, one spec per vulnerability class.

A confirmed exploit is a single witness ("user B fetched note 2"). The
regression test must instead protect the property that witness violated
("no principal can read or modify an object it does not own"), so a variant
payload, another operation on the same object, or a later refactor cannot
reintroduce the bug unnoticed.

Each VulnerabilityClassSpec turns a verified Finding plus its sandbox exploit
evidence into a SecurityInvariant, and contributes class-specific guidance to
the generator prompt. Adding a class means adding a spec to ``CLASS_SPECS``.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Optional
from urllib.parse import parse_qsl, urlsplit

from openhack.categories import normalize_category
from .models import SecurityInvariant

if TYPE_CHECKING:
    from openhack.agents.session import Finding


class MissingEvidenceError(Exception):
    """The finding has no exploit evidence to derive an invariant from."""


class VulnerabilityClassSpec(ABC):
    """Extension point for a vulnerability class."""

    key: str = ""
    label: str = ""
    cwe: str = ""
    # Whether a passing "legitimate use still works" test is mandatory. It is
    # what stops an always-denying (or broken) endpoint from looking fixed.
    requires_control_test: bool = True

    @abstractmethod
    def matches(self, finding: "Finding") -> bool:
        """Whether this spec handles the finding's vulnerability class."""

    @abstractmethod
    def extract_invariant(self, finding: "Finding") -> SecurityInvariant:
        """Derive the violated invariant; raise MissingEvidenceError if unable."""

    @abstractmethod
    def generation_guidance(self, invariant: SecurityInvariant) -> str:
        """Class-specific instructions appended to the generator prompt."""


# ── Request parsing helpers ─────────────────────────────────────────

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_ID_SEGMENT = re.compile(rf"^(?:\d+|{_UUID}|[0-9a-fA-F]{{24}}|[0-9a-fA-F]{{32}})$")
# A templated segment in a PoC f-string, e.g. "{note_id}".
_PLACEHOLDER_SEGMENT = re.compile(r"^\{([A-Za-z_]\w*)\}$")
_SNAKE_ID_KEY = re.compile(r"(?:^|_)(?:id|uuid|pk)$", re.IGNORECASE)
_CAMEL_ID_KEY = re.compile(r"[a-z0-9](?:Id|ID|Uuid)$")
# `requests.get("…")`, `session.post(BASE + "…")`, `client.delete(f"{BASE}/…")`
_POC_REQUEST = re.compile(
    r"""\b\w+\.(get|post|put|patch|delete)\(\s*(?:[A-Za-z_][\w.]*\s*\+\s*)?f?["']([^"']+)["']""",
    re.IGNORECASE,
)
_CURL_METHOD = re.compile(r"(?:-X|--request)\s*([A-Za-z]+)")
_URL = re.compile(r"https?://[^\s\"']+")

_OPERATIONS = {
    "GET": "read", "HEAD": "read",
    "PUT": "update", "PATCH": "update",
    "DELETE": "delete",
    "POST": "act on",
}


def _singular(word: str) -> str:
    word = word.strip().lower()
    if word.endswith("ies") and len(word) > 3:
        return word[:-3] + "y"
    if word.endswith("sses"):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss") and len(word) > 1:
        return word[:-1]
    return word


def _is_id_key(key: str) -> bool:
    return bool(_SNAKE_ID_KEY.search(key) or _CAMEL_ID_KEY.search(key))


def _path_only(raw: str) -> str:
    """Strip scheme/host (or a leading ``{BASE_URL}`` placeholder) from a URL."""
    raw = (raw or "").strip()
    if raw.startswith(("http://", "https://")):
        parts = urlsplit(raw)
        return parts.path + (f"?{parts.query}" if parts.query else "")
    if raw.startswith("{") and "}/" in raw:
        return raw[raw.index("}/") + 1:]
    return raw


def request_from_poc(poc: str) -> tuple[str, str]:
    """Best-effort (method, path) of the exploit request in a Python/curl PoC.

    PoCs usually register/log in first, so the exploit is taken to be the last
    request that addresses an object identifier (else simply the last request).
    """
    if not poc:
        return "", ""
    candidates: list[tuple[str, str]] = []
    for match in _POC_REQUEST.finditer(poc):
        path = _path_only(match.group(2))
        if path.startswith("/"):
            candidates.append((match.group(1).upper(), path))
    for line in poc.splitlines():
        if "curl" not in line:
            continue
        url = _URL.search(line)
        if url:
            method = _CURL_METHOD.search(line)
            candidates.append(((method.group(1).upper() if method else "GET"), _path_only(url.group(0))))
    if not candidates:
        return "", ""
    with_ids = [c for c in candidates if templatize_path(c[1])[1] or _query_id_param(c[1])]
    return (with_ids or candidates)[-1]


def templatize_path(path: str) -> tuple[str, Optional[str], str]:
    """Replace identifier segments with named placeholders.

    Returns (template, object_param, resource). ``/api/users/7/notes/42``
    becomes ``/api/users/{user_id}/notes/{note_id}`` with object_param
    ``note_id`` — the last identifier names the object being accessed.
    """
    split = urlsplit(path)
    segments = [s for s in split.path.split("/") if s]
    out: list[str] = []
    object_param: Optional[str] = None
    resource = ""
    for i, segment in enumerate(segments):
        placeholder = _PLACEHOLDER_SEGMENT.match(segment)
        if _ID_SEGMENT.match(segment) or placeholder:
            owner = _singular(segments[i - 1]) if i > 0 else "object"
            object_param = placeholder.group(1) if placeholder else f"{owner}_id"
            resource = owner
            out.append("{" + object_param + "}")
        else:
            out.append(segment)
    if not resource:
        nouns = [
            s for s in segments
            if not _ID_SEGMENT.match(s) and not _PLACEHOLDER_SEGMENT.match(s)
        ]
        resource = _singular(nouns[-1]) if nouns else "object"
    template = "/" + "/".join(out)
    return template, object_param, resource


def _query_id_param(path: str) -> Optional[str]:
    for key, _value in parse_qsl(urlsplit(path).query, keep_blank_values=True):
        if _is_id_key(key):
            return key
    return None


def _body_id_param(body: Optional[str]) -> Optional[str]:
    if not body:
        return None
    for key in re.findall(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:', body):
        if _is_id_key(key):
            return key
    return None


# ── IDOR / broken object-level authorization ────────────────────────

class IdorSpec(VulnerabilityClassSpec):
    key = "idor"
    label = "IDOR / broken object-level authorization"
    cwe = "CWE-639"
    requires_control_test = True

    def matches(self, finding: "Finding") -> bool:
        return normalize_category(finding.category or "") == "IDOR"

    def extract_invariant(self, finding: "Finding") -> SecurityInvariant:
        evidence = finding.exploit_evidence or {}
        if not evidence:
            raise MissingEvidenceError(
                "the finding was not verified by sandbox exploitation, so there is "
                "no exploit evidence to derive the violated invariant from "
                "(enable sandbox verification with sandbox_enabled or /verify sandbox)"
            )
        request = evidence.get("exploit_request") or {}
        method = str(request.get("method") or "").upper()
        path = _path_only(str(request.get("path") or ""))
        if not path:
            poc_method, path = request_from_poc(evidence.get("working_poc") or "")
            method = method or poc_method
        if not path:
            raise MissingEvidenceError(
                "the sandbox evidence does not identify the HTTP request that "
                "exploited the object reference"
            )
        method = method or "GET"
        template, object_param, resource = templatize_path(path)
        location = "path" if object_param else None
        if not object_param:
            object_param = _query_id_param(path)
            location = "query" if object_param else None
        if not object_param:
            object_param = _body_id_param(request.get("body"))
            location = "body" if object_param else None
        operation = _OPERATIONS.get(method, "access")
        query = urlsplit(path).query
        endpoint = f"{method} {template}" + (f"?{query}" if location == "query" else "")
        statement = (
            f"Object-level authorization ({self.cwe}): an authenticated principal "
            f"must not be able to {operation} a {resource} owned by another principal "
            f"through {endpoint}, no matter which {object_param or 'object identifier'} "
            "it supplies. Only principals authorized for the object (its owner) may "
            f"{operation} it."
        )
        denial = (
            "A request for another principal's object is rejected (401, 403 or 404), "
            "the response does not disclose the object's data, and a rejected "
            "write/delete leaves the object unchanged."
        )
        control = (
            f"The owner can still {operation} their own {resource} through the same "
            "endpoint (proves the denial comes from authorization, not a broken route)."
        )
        summary = (evidence.get("evidence") or "").strip()
        location_str = finding.file_path or ""
        if finding.line_number:
            location_str += f":{finding.line_number}"
        return SecurityInvariant(
            vulnerability_class=self.key,
            cwe=self.cwe,
            statement=statement,
            operation=operation,
            resource=resource,
            method=method,
            path_template=template,
            example_path=path,
            object_param=object_param,
            param_location=location,
            denial=denial,
            control=control,
            evidence=summary[:800],
            source_location=location_str,
        )

    def generation_guidance(self, invariant: SecurityInvariant) -> str:
        param = invariant.object_param or "the object identifier"
        return f"""### Class-specific requirements: {self.label}
- Create TWO distinct principals through the application's own mechanisms
  (existing fixtures, factories, registration/login endpoints): an owner and an
  unrelated second principal. Never reuse tokens, cookies or passwords from the
  exploit evidence; use clearly fake test credentials.
- Have the owner create (or own) a {invariant.resource} with a distinctive marker
  value in it, through the application, so ownership is real.
- Control test(s): the owner can {invariant.operation} their own {invariant.resource}
  via `{invariant.method} {invariant.path_template}` and gets a success response.
- Invariant test(s): the second principal supplies the owner's {param} to
  `{invariant.method} {invariant.path_template}` and MUST be denied. Assert the
  status is one of 401/403/404 (accept any of them — never pin the one status a
  particular fix happens to return) AND that the owner's marker value is absent
  from the response body.
- Protect the invariant, not the payload: if the same object is reachable through
  other operations on that route (read/update/delete), add an invariant test per
  operation. For write/delete attempts, re-read the object as the owner afterwards
  and assert it is unchanged.
"""


CLASS_SPECS: list[VulnerabilityClassSpec] = [IdorSpec()]


def spec_for_finding(finding: "Finding") -> Optional[VulnerabilityClassSpec]:
    for spec in CLASS_SPECS:
        if spec.matches(finding):
            return spec
    return None


def supported_classes() -> list[str]:
    return [spec.label for spec in CLASS_SPECS]
