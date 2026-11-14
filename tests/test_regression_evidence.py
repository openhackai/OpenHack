"""Exploit evidence is redacted before it is persisted or shown to an LLM."""

import json

import pytest

from openhack.regression.evidence import find_secrets, sanitize_exploit_evidence, scrub_text
from tests.regression_support import sandbox_result


def test_sanitized_evidence_keeps_the_exploit_shape_but_no_credentials():
    record = sanitize_exploit_evidence(sandbox_result(
        exploit_request={
            "method": "put",
            "path": "http://localhost:40123/api/notes/1",
            "headers": {"Authorization": "Bearer live-token-123", "Cookie": "session=abc",
                        "X-CSRF-Token": "t0k", "Accept": "application/json"},
            "json_body": {"body": "tampered", "password": "hunter2!"},
        },
    ))
    serialized = json.dumps(record)

    assert record["source"] == "sandbox"
    assert record["exploit_request"]["method"] == "PUT"
    assert record["exploit_request"]["path"] == "http://localhost:40123/api/notes/1"
    assert record["exploit_request"]["headers"]["Accept"] == "application/json"
    for header in ("Authorization", "Cookie", "X-CSRF-Token"):
        assert record["exploit_request"]["headers"][header] == "[REDACTED]"
    assert "tampered" in record["exploit_request"]["body"]
    for secret in ("live-token-123", "session=abc", "hunter2!", "eyJhbGciOiJIUzI1NiJ9"):
        assert secret not in serialized


def test_poc_and_evidence_text_are_scrubbed_and_bounded():
    record = sanitize_exploit_evidence(sandbox_result(
        working_poc="requests.get(u, headers={'Authorization': 'Bearer sk-livekeylivekeylivekey123'})\n" * 200,
    ))
    assert "sk-livekey" not in record["working_poc"]
    assert len(record["working_poc"]) <= 4000
    assert "eyJhbGci" not in record["evidence"]


def test_empty_results_carry_no_evidence():
    assert sanitize_exploit_evidence(None) is None
    assert sanitize_exploit_evidence({}) is None
    assert sanitize_exploit_evidence({"evidence": "  ", "exploit_request": {}}) is None


@pytest.mark.parametrize("text,leak", [
    ("Cookie: session=deadbeef; csrftoken=abc", "deadbeef"),
    ('{"password": "s3cr3t-pass"}', "s3cr3t-pass"),
    ("token = 'abcd1234efgh'", "abcd1234efgh"),
    ("Authorization: Bearer abc.def.ghi", "abc.def.ghi"),
])
def test_scrub_text(text, leak):
    assert leak not in scrub_text(text)


@pytest.mark.parametrize("code,expected", [
    ("TOKEN = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl'", "JSON Web Token"),
    ("key = 'AKIAIOSFODNN7EXAMPLE'", "AWS access key"),
    ("api = 'sk-abcdefghijklmnopqrstuvwxyz'", "API secret key"),
    ("headers = {'Authorization': 'Bearer 3f9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c'}", "literal bearer token"),
    ("headers = {'Authorization': 'Bearer [REDACTED]'}", "redacted evidence placeholder"),
    ("-----BEGIN RSA PRIVATE KEY-----", "private key"),
])
def test_find_secrets_flags_credentials(code, expected):
    assert expected in find_secrets(code)


def test_find_secrets_allows_ordinary_test_credentials():
    code = (
        'headers = {"Authorization": f"Bearer {token}"}\n'
        'client.post("/api/login", json={"username": "alice", "password": "correct-horse-battery"})\n'
    )
    assert find_secrets(code) == []
