"""Security-invariant extraction from verified findings and exploit evidence."""

import pytest

from openhack.agents.session import Finding
from openhack.regression.evidence import sanitize_exploit_evidence
from openhack.regression.invariants import (
    IdorSpec,
    MissingEvidenceError,
    request_from_poc,
    spec_for_finding,
    supported_classes,
    templatize_path,
)
from tests.regression_support import sandbox_result, verified_idor_finding


def test_idor_invariant_from_sandbox_exploit_request():
    invariant = IdorSpec().extract_invariant(verified_idor_finding())

    assert invariant.vulnerability_class == "idor"
    assert invariant.cwe == "CWE-639"
    assert invariant.method == "GET"
    assert invariant.path_template == "/api/notes/{note_id}"
    assert invariant.example_path == "/api/notes/1"
    assert invariant.object_param == "note_id"
    assert invariant.param_location == "path"
    assert invariant.resource == "note"
    assert invariant.operation == "read"
    assert invariant.source_location == "app.py:70"
    # The invariant generalizes the witness: it is about every principal and
    # identifier, and it states what must keep working.
    assert "owned by another principal" in invariant.statement
    assert "no matter which note_id" in invariant.statement
    assert "401, 403 or 404" in invariant.denial
    assert "owner can still read" in invariant.control


def test_invariant_evidence_is_redacted():
    invariant = IdorSpec().extract_invariant(verified_idor_finding())
    assert "eyJhbGci" not in invariant.evidence
    assert "xyzzy-live-session-token" not in invariant.evidence


@pytest.mark.parametrize("method,operation", [
    ("PUT", "update"), ("PATCH", "update"), ("DELETE", "delete"), ("POST", "act on"),
])
def test_operation_follows_http_method(method, operation):
    result = sandbox_result(exploit_request={"method": method, "path": "/api/notes/9"})
    finding = verified_idor_finding(exploit_evidence=sanitize_exploit_evidence(result))
    assert IdorSpec().extract_invariant(finding).operation == operation


def test_nested_and_uuid_identifiers_are_templated():
    uuid = "3f2b8c1e-9d4a-4b6f-8e2a-1c5d7f9a0b3e"
    template, param, resource = templatize_path(f"/api/users/42/documents/{uuid}")
    assert template == "/api/users/{user_id}/documents/{document_id}"
    assert param == "document_id"
    assert resource == "document"
    assert templatize_path("/api/categories/7")[2] == "category"


def test_query_parameter_identifier():
    result = sandbox_result(exploit_request={"method": "GET", "path": "/api/invoice?invoiceId=55&format=pdf"})
    invariant = IdorSpec().extract_invariant(
        verified_idor_finding(exploit_evidence=sanitize_exploit_evidence(result))
    )
    assert invariant.object_param == "invoiceId"
    assert invariant.param_location == "query"
    assert invariant.resource == "invoice"


def test_body_identifier():
    result = sandbox_result(exploit_request={
        "method": "POST", "path": "/api/orders/cancel", "json_body": {"order_id": 77, "valid": True},
    })
    invariant = IdorSpec().extract_invariant(
        verified_idor_finding(exploit_evidence=sanitize_exploit_evidence(result))
    )
    assert invariant.object_param == "order_id"
    assert invariant.param_location == "body"


def test_falls_back_to_the_working_poc_when_the_request_is_missing():
    result = sandbox_result(exploit_request=None)
    invariant = IdorSpec().extract_invariant(
        verified_idor_finding(exploit_evidence=sanitize_exploit_evidence(result))
    )
    assert (invariant.method, invariant.path_template) == ("GET", "/api/notes/{note_id}")


@pytest.mark.parametrize("poc,expected", [
    ("requests.delete(f'{BASE}/api/notes/{note_id}', headers=h)", ("DELETE", "/api/notes/{note_id}")),
    ("r = session.put(BASE_URL + '/api/notes/3', json={})", ("PUT", "/api/notes/3")),
    ("curl -s -H 'Authorization: Bearer x' -X PATCH http://localhost:3000/api/notes/4", ("PATCH", "/api/notes/4")),
    ("value = data.get('key')\nrequests.get('http://h:1/api/a/1')", ("GET", "/api/a/1")),
    ("print('nothing to see')", ("", "")),
])
def test_request_from_poc(poc, expected):
    assert request_from_poc(poc) == expected


def test_finding_without_exploit_evidence_is_missing_evidence():
    finding = verified_idor_finding(exploit_evidence=None, source=None)
    with pytest.raises(MissingEvidenceError, match="not verified by sandbox exploitation"):
        IdorSpec().extract_invariant(finding)


def test_evidence_that_does_not_identify_the_request_is_missing_evidence():
    evidence = sanitize_exploit_evidence(
        {"evidence": "It worked, I saw someone else's data.", "working_poc": "print('ok')"}
    )
    with pytest.raises(MissingEvidenceError, match="does not identify the HTTP request"):
        IdorSpec().extract_invariant(verified_idor_finding(exploit_evidence=evidence))


@pytest.mark.parametrize("category", ["IDOR", "Insecure Direct Object Reference", "broken object level authorization"])
def test_idor_categories_are_supported(category):
    assert isinstance(spec_for_finding(Finding(category=category, severity="high", title="t",
                                               description="d", file_path="a.py")), IdorSpec)


@pytest.mark.parametrize("category", ["XSS", "SQL Injection", "SSRF", "Authentication Bypass"])
def test_other_classes_are_unsupported(category):
    finding = Finding(category=category, severity="high", title="t", description="d", file_path="a.py")
    assert spec_for_finding(finding) is None
    assert supported_classes() == ["IDOR / broken object-level authorization"]


def test_generation_guidance_targets_the_invariant_not_a_payload():
    spec = IdorSpec()
    guidance = spec.generation_guidance(spec.extract_invariant(verified_idor_finding()))
    assert "TWO distinct principals" in guidance
    assert "accept any of them" in guidance
    assert "Never reuse tokens" in guidance
    assert "GET /api/notes/{note_id}" in guidance
