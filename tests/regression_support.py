"""Shared fixtures for the security regression test generation tests.

The IDOR fixture app lives in tests/fixtures/regression/idor_notes_app: a
Flask notes API with its own pytest suite (``vulnerable/``) and the remediated
``fixed_app.py``. ``make_fixture_repo`` turns it into a real git repository
whose main branch is vulnerable and whose ``fix/idor-notes`` branch holds the
fix, exactly as a developer's repository would look after remediation.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Optional, Union
from xml.sax.saxutils import escape, quoteattr

from openhack.agents.llm import LLMResponse, ToolCall
from openhack.agents.session import Finding
from openhack.regression.evidence import sanitize_exploit_evidence
from openhack.sandbox.container_runner import ContainerRunResult
from openhack.sandbox.orchestrator import SandboxError

FIXTURE_APP = Path(__file__).parent / "fixtures" / "regression" / "idor_notes_app"
FIX_BRANCH = "fix/idor-notes"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
    ).stdout.strip()


def make_fixture_repo(tmp_path: Path) -> dict:
    """Create the IDOR fixture as a git repo; returns paths and commit ids."""
    repo = tmp_path / "notes-app"
    shutil.copytree(FIXTURE_APP / "vulnerable", repo)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "dev@example.invalid")
    git(repo, "config", "user.name", "dev")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "Notes API")
    vulnerable_sha = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "-b", FIX_BRANCH)
    shutil.copy(FIXTURE_APP / "fixed_app.py", repo / "app.py")
    git(repo, "commit", "-q", "-am", "Check note ownership")
    fixed_sha = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "main")
    return {"repo": repo, "vulnerable_sha": vulnerable_sha, "fixed_sha": fixed_sha}


def sandbox_result(**overrides) -> dict:
    """An `exploitable` result as SandboxVerifierAgent reports it."""
    result = {
        "finding_index": 0,
        "status": "exploitable",
        "confidence": "high",
        "working_poc": (
            "import requests\n"
            "BASE = 'http://localhost:40123'\n"
            "s = requests.post(BASE + '/api/login', json={'username': 'mallory', "
            "'password': 'hunter2!'})\n"
            "tok = s.json()['token']\n"
            "r = requests.get(BASE + '/api/notes/1', headers={'Authorization': 'Bearer ' + tok})\n"
            "print(r.status_code, r.text)\n"
        ),
        "evidence": (
            "Logged in as mallory (Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJtYWxsb3J5In0.c2lnbmF0dXJlLXZhbHVl) "
            "and fetched note 1 owned by alice: HTTP 200 {\"body\": \"alice's private note\"}"
        ),
        "attempts_made": 2,
        "exploit_request": {
            "method": "GET",
            "path": "/api/notes/1",
            "headers": {
                "Authorization": "Bearer xyzzy-live-session-token-0123456789",
                "Accept": "application/json",
            },
        },
    }
    result.update(overrides)
    return result


def verified_idor_finding(**overrides) -> Finding:
    finding = Finding(
        category="IDOR",
        severity="high",
        title="IDOR in app.py",
        description="GET/PUT/DELETE /api/notes/<id> never check that the note belongs to the caller.",
        file_path="app.py",
        line_number=70,
        code_snippet="note = notes.get(note_id)\nreturn jsonify(note)",
        fix="Look the note up by id AND owner; return 404 when the caller does not own it.",
        validated=True,
        source="sandbox_verified",
        exploit_evidence=sanitize_exploit_evidence(sandbox_result()),
    )
    for key, value in overrides.items():
        setattr(finding, key, value)
    return finding


# ── Generated test sources the scripted LLM submits ─────────────────

GOOD_TEST = '''"""Object-level authorization regression test for notes (CWE-639).

A user must never be able to read, modify or delete another user's note by
supplying its id; the owner keeps full access.
"""

import pytest

DENIED = (401, 403, 404)
SECRET_BODY = "owner-only-marker-7f3a"


@pytest.fixture
def owner_note(client, auth_headers):
    owner = auth_headers("note-owner")
    created = client.post("/api/notes", json={"title": "private", "body": SECRET_BODY}, headers=owner)
    assert created.status_code == 201, "setup: the owner must be able to create a note"
    return owner, created.get_json()["id"]


def test_owner_can_read_own_note(client, owner_note):
    owner, note_id = owner_note
    response = client.get(f"/api/notes/{note_id}", headers=owner)
    assert response.status_code == 200, "the owner must keep access to their own note"
    assert response.get_json()["body"] == SECRET_BODY


def test_other_user_cannot_read_note(client, auth_headers, owner_note):
    _, note_id = owner_note
    intruder = auth_headers("intruder")
    response = client.get(f"/api/notes/{note_id}", headers=intruder)
    assert response.status_code in DENIED, f"another user read a note they do not own (HTTP {response.status_code})"
    assert SECRET_BODY not in response.get_data(as_text=True), "note contents leaked to another user"


def test_other_user_cannot_modify_note(client, auth_headers, owner_note):
    owner, note_id = owner_note
    intruder = auth_headers("intruder")
    response = client.put(f"/api/notes/{note_id}", json={"body": "tampered"}, headers=intruder)
    assert response.status_code in DENIED, f"another user modified a note they do not own (HTTP {response.status_code})"
    after = client.get(f"/api/notes/{note_id}", headers=owner)
    assert after.get_json()["body"] == SECRET_BODY, "a rejected update must leave the note unchanged"


def test_other_user_cannot_delete_note(client, auth_headers, owner_note):
    owner, note_id = owner_note
    intruder = auth_headers("intruder")
    response = client.delete(f"/api/notes/{note_id}", headers=intruder)
    assert response.status_code in DENIED, f"another user deleted a note they do not own (HTTP {response.status_code})"
    assert client.get(f"/api/notes/{note_id}", headers=owner).status_code == 200, "a rejected delete must leave the note in place"
'''

GOOD_INVARIANT_TESTS = [
    "test_other_user_cannot_read_note",
    "test_other_user_cannot_modify_note",
    "test_other_user_cannot_delete_note",
]
GOOD_CONTROL_TESTS = ["test_owner_can_read_own_note"]

# Plausible but useless: it checks authentication, not object ownership, so it
# passes against the vulnerable code and must be rejected.
WEAK_TEST = '''"""Notes require authentication."""


def test_owner_can_list_notes(client, auth_headers):
    headers = auth_headers("note-owner")
    assert client.get("/api/notes", headers=headers).status_code == 200


def test_anonymous_cannot_read_note(client, auth_headers):
    owner = auth_headers("note-owner")
    created = client.post("/api/notes", json={"title": "t", "body": "b"}, headers=owner)
    response = client.get(f"/api/notes/{created.get_json()['id']}")
    assert response.status_code in (401, 403, 404), "anonymous users must not read notes"
'''
WEAK_INVARIANT_TESTS = ["test_anonymous_cannot_read_note"]
WEAK_CONTROL_TESTS = ["test_owner_can_list_notes"]

TEST_PATH = "tests/test_security_idor_note.py"


def submit(code: str = GOOD_TEST, path: str = TEST_PATH,
           invariant: Optional[list[str]] = None, control: Optional[list[str]] = None,
           call_id: str = "submit-1") -> LLMResponse:
    return LLMResponse(
        tool_calls=[ToolCall(
            id=call_id,
            name="submit_regression_test",
            arguments={
                "path": path,
                "code": code,
                "invariant_tests": GOOD_INVARIANT_TESTS if invariant is None else invariant,
                "control_tests": GOOD_CONTROL_TESTS if control is None else control,
                "summary": "Users cannot read, modify or delete notes they do not own.",
            },
        )],
        finish_reason="tool_calls",
        model_call_id=call_id,
    )


def submit_weak(call_id: str = "submit-weak") -> LLMResponse:
    return submit(WEAK_TEST, invariant=WEAK_INVARIANT_TESTS, control=WEAK_CONTROL_TESTS, call_id=call_id)


def read_file_call(path: str = "tests/conftest.py", call_id: str = "read-1") -> LLMResponse:
    return LLMResponse(
        tool_calls=[ToolCall(id=call_id, name="read_file", arguments={"path": path})],
        finish_reason="tool_calls",
        model_call_id=call_id,
    )


class ScriptedLLM:
    """Returns pre-scripted responses and records every request it receives."""

    model = "scripted-model"
    provider = "scripted"

    def __init__(self, responses: list[Union[LLMResponse, Exception]]):
        self.responses = list(responses)
        self.calls = 0
        self.requests: list[list] = []
        self.event_callback = None
        self.total_cost = 0.0
        self.total_tokens = 0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.prompt_cache_key = None

    async def chat(self, messages=None, tools=None, system=None, **kwargs):
        self.requests.append(list(messages or []))
        if self.calls >= len(self.responses):
            raise AssertionError("ScriptedLLM ran out of scripted responses")
        response = self.responses[self.calls]
        self.calls += 1
        if isinstance(response, Exception):
            raise response
        self.total_cost += 0.01
        self.total_tokens += 100
        return response

    def last_user_text(self) -> str:
        for message in reversed(self.requests[-1] if self.requests else []):
            if message.role == "user":
                return message.content or ""
        return ""


# ── Scan stages ─────────────────────────────────────────────────────

class StubStage:
    """An LLM-backed scan stage replaced by a canned result."""

    result: dict = {}

    def __init__(self, *args, **kwargs):
        self.total_cost = 0.0
        self.total_tokens = 0
        self.total_input_tokens = 0
        self.total_output_tokens = 0

    async def run(self, task, context=None):
        return self.result


HUNTER_FINDING = {
    "category": "IDOR",
    "severity": "high",
    "file_path": "app.py",
    "line_number": 70,
    "description": "GET /api/notes/<id> returns any note without checking ownership.",
    "code_snippet": "note = notes.get(note_id)",
}


def install_scan_stubs(monkeypatch, regression_llm) -> dict:
    """Stub the LLM-backed stages (recon, hunters, validators, sandbox verifier)
    with canned results for the notes-app IDOR, and route the regression-test
    agent to ``regression_llm``. Everything else in the scan stays real.
    Returns the LLMs handed out, keyed by agent type."""
    import openhack.agents.coordinator as coordinator_mod

    class Recon(StubStage):
        result = {"summary": ""}

    class Hunters(StubStage):
        result = {"findings": [dict(HUNTER_FINDING)], "files_analyzed": ["app.py"]}

    class Validators(StubStage):
        result = {"validated_findings": [{
            "original_index": 0, "status": "confirmed", "confidence": "high",
            "poc": "GET /api/notes/1 as another user", "fix": "Check note ownership.", "cvss_score": 8.1,
        }], "false_positives": []}

    class Sandbox(StubStage):
        result = {"exploitable": [sandbox_result()], "not_exploitable": []}

    def no_attack_surface(*args, **kwargs):
        raise RuntimeError("attack surface discovery is not needed here")

    monkeypatch.setattr(coordinator_mod, "ReconAgent", Recon)
    monkeypatch.setattr(coordinator_mod, "HunterSwarmAgent", Hunters)
    monkeypatch.setattr(coordinator_mod, "ValidatorSwarmAgent", Validators)
    monkeypatch.setattr(coordinator_mod, "SandboxVerifierSwarmAgent", Sandbox)
    monkeypatch.setattr(coordinator_mod, "discover_attack_surface", no_attack_surface)

    llms: dict = {}

    def create_llm(self, agent_type):
        llms[agent_type] = regression_llm if agent_type == "regression_test" else ScriptedLLM([])
        return llms[agent_type]

    monkeypatch.setattr(coordinator_mod.CoordinatorAgent, "_create_llm_for_agent", create_llm)
    return llms


# ── Fake container runner (writes real JUnit XML) ───────────────────

def names_in(code: str) -> list[str]:
    """Test function names defined in generated code."""
    return re.findall(r"^def (test_\w+)\(", code, flags=re.MULTILINE)


def junit_xml(cases: dict[str, str], classname: str = "tests.test_security_idor_note") -> str:
    """JUnit XML in pytest's format. Outcomes: passed, failed (assertion),
    crashed (non-assertion failure), error, skipped."""
    rows = []
    for name, outcome in cases.items():
        attrs = f"classname={quoteattr(classname)} name={quoteattr(name)} time=\"0.01\""
        if outcome == "passed":
            rows.append(f"<testcase {attrs}/>")
        elif outcome == "failed":
            msg = "AssertionError: another user read a note they do not own (HTTP 200)"
            body = f"def {name}(...):\n>       assert response.status_code in DENIED\nE       {msg}"
            rows.append(f"<testcase {attrs}><failure message={quoteattr(msg)}>{escape(body)}</failure></testcase>")
        elif outcome == "crashed":
            msg = "KeyError: 'id'"
            rows.append(f"<testcase {attrs}><failure message={quoteattr(msg)}>E   {escape(msg)}</failure></testcase>")
        elif outcome == "error":
            msg = 'failed on setup with "fixture \'database\' not found"'
            rows.append(f"<testcase {attrs}><error message={quoteattr(msg)}>{escape(msg)}</error></testcase>")
        elif outcome == "skipped":
            rows.append(f"<testcase {attrs}><skipped type=\"pytest.skip\" message=\"skipped\"/></testcase>")
    failures = sum(o in ("failed", "crashed") for o in cases.values())
    return (
        '<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest" '
        f'errors="0" failures="{failures}" skipped="0" tests="{len(cases)}">'
        + "".join(rows) + "</testsuite></testsuites>"
    )


COLLECTION_ERROR_XML = (
    '<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest" errors="1" '
    'failures="0" skipped="0" tests="1"><testcase classname="" name="tests.test_security_idor_note">'
    '<error message="collection failure">ImportError while importing test module: '
    "ModuleNotFoundError: No module named 'notes'</error></testcase></testsuite></testsuites>"
)

Behavior = Callable[[str, str, int], Union[dict, str]]


def realistic_idor(label: str, code: str, run_index: int) -> dict:
    """What the GOOD/WEAK tests really do against the fixture app."""
    outcomes = {}
    for name in names_in(code):
        if "other_user_cannot" in name:
            outcomes[name] = "failed" if label == "vulnerable" else "passed"
        else:
            outcomes[name] = "passed"
    return outcomes


class FakeRunner:
    """Stands in for ContainerTestRunner. ``behavior(label, code, run_index)``
    returns {test: outcome}, or "timeout" / "collection_error" / "no_results"."""

    def __init__(self, behavior: Behavior = realistic_idor, available: bool = True,
                 build_error: Optional[str] = None):
        self.behavior = behavior
        self.available = available
        self.build_error = build_error
        self.builds: list[tuple[str, str]] = []
        self.runs: list[dict] = []
        self.images_removed = False

    async def ensure_available(self) -> None:
        if not self.available:
            raise SandboxError("Docker is required to validate regression tests but is not available: daemon down")

    async def build_image(self, context_dir: Path, dockerfile: str, tag: str) -> None:
        self.builds.append((tag, dockerfile))
        if self.build_error:
            raise SandboxError(f"sandbox image build failed: {self.build_error}")

    async def run_test(self, image, files, command, workdir="/workspace", results_path="/openhack-results"):
        label = "vulnerable" if "-vulnerable-" in image else "remediated"
        code = next(iter(files.values()))
        run_index = sum(1 for r in self.runs if r["label"] == label and r["code"] == code)
        self.runs.append({"label": label, "image": image, "files": dict(files),
                          "command": list(command), "workdir": workdir, "code": code})
        results = Path(tempfile.mkdtemp(prefix="fake-results-"))
        outcome = self.behavior(label, code, run_index)
        if outcome == "timeout":
            return ContainerRunResult(None, True, results)
        if outcome == "no_results":
            (results / "output.txt").write_text("ERROR: usage: pytest [options]\n")
            return ContainerRunResult(4, False, results)
        if outcome == "collection_error":
            (results / "junit.xml").write_text(COLLECTION_ERROR_XML)
            (results / "output.txt").write_text("ModuleNotFoundError: No module named 'notes'\n")
            return ContainerRunResult(2, False, results)
        (results / "junit.xml").write_text(junit_xml(outcome))
        (results / "output.txt").write_text("collected tests\n")
        failed = any(o != "passed" for o in outcome.values())
        return ContainerRunResult(1 if failed else 0, False, results)

    async def remove_images(self) -> None:
        self.images_removed = True

    def runs_for(self, label: str) -> list[dict]:
        return [r for r in self.runs if r["label"] == label]
