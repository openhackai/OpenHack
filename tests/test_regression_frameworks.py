"""Test-framework detection, policy checks and result parsing (pytest adapter)."""

import json
import shutil

import pytest

from openhack.regression.frameworks import (
    PytestAdapter,
    UnsupportedFrameworkError,
    detect_framework,
)
from openhack.regression.invariants import IdorSpec
from tests.conftest import write_file
from tests.regression_support import (
    COLLECTION_ERROR_XML,
    FIXTURE_APP,
    GOOD_TEST,
    junit_xml,
    verified_idor_finding,
)

adapter = PytestAdapter()


@pytest.fixture
def notes_repo(tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE_APP / "vulnerable", repo)
    return repo


def test_detects_the_repositorys_own_pytest_conventions(notes_repo):
    found, profile = detect_framework(notes_repo, "app.py")

    assert found.name == "pytest"
    assert profile.project_root == "."
    assert profile.test_dir == "tests"
    assert profile.config_file == "pytest.ini"
    assert profile.file_patterns == ["test_*.py", "*_test.py"]
    assert profile.fixtures == {"client": "tests/conftest.py", "auth_headers": "tests/conftest.py"}
    assert profile.example_tests == ["tests/test_notes.py"]
    assert profile.dependency_files == ["requirements.txt"]
    assert profile.app_frameworks == ["flask"]
    assert profile.framework_in_dependencies is True
    assert profile.runtime_version == "3.12"


def test_pyproject_configuration_and_custom_patterns(tmp_path):
    write_file(tmp_path, "pyproject.toml", """
[project]
name = "svc"
requires-python = ">=3.10,<3.12"
dependencies = ["fastapi"]
[project.optional-dependencies]
test = ["pytest", "httpx"]
[tool.pytest.ini_options]
testpaths = ["checks"]
python_files = ["check_*.py"]
""")
    write_file(tmp_path, "checks/check_api.py", "def test_x():\n    assert True\n")
    write_file(tmp_path, ".python-version", "3.11.9\n")
    _, profile = detect_framework(tmp_path)

    assert profile.config_file == "pyproject.toml"
    assert profile.test_dir == "checks"
    assert profile.file_patterns == ["check_*.py"]
    assert profile.installable_project is True
    assert profile.install_extras == ["test"]
    assert profile.app_frameworks == ["fastapi"]
    assert profile.runtime_version == "3.11"


def test_monorepo_picks_the_project_containing_the_finding(tmp_path):
    write_file(tmp_path, "docs/requirements.txt", "mkdocs\n")
    write_file(tmp_path, "services/api/requirements.txt", "flask\npytest\n")
    write_file(tmp_path, "services/api/tests/conftest.py", "import pytest\n")
    write_file(tmp_path, "services/api/tests/test_api.py", "def test_ok():\n    assert True\n")
    _, profile = detect_framework(tmp_path, "services/api/app.py")
    assert profile.project_root == "services/api"
    assert profile.test_dir == "tests"


def test_javascript_suites_are_reported_as_unsupported(tmp_path):
    package = {"devDependencies": {"jest": "^29"}, "scripts": {"test": "jest"}}
    write_file(tmp_path, "package.json", json.dumps(package))
    with pytest.raises(UnsupportedFrameworkError, match="`jest`.*not supported yet.*pytest"):
        detect_framework(tmp_path)


def test_django_runner_without_pytest_is_unsupported(tmp_path):
    write_file(tmp_path, "manage.py", "import django\n")
    write_file(tmp_path, "requirements.txt", "django\n")
    with pytest.raises(UnsupportedFrameworkError, match="Django's built-in test runner"):
        detect_framework(tmp_path)


def test_repository_without_tests_is_unsupported(tmp_path):
    write_file(tmp_path, "app.py", "print('hi')\n")
    with pytest.raises(UnsupportedFrameworkError, match="no existing test suite was detected"):
        detect_framework(tmp_path)


def test_node_modules_are_not_traversed(tmp_path):
    write_file(tmp_path, "node_modules/pkg/conftest.py", "import pytest\n")
    write_file(tmp_path, "node_modules/pkg/tests/test_x.py", "def test_x(): pass\n")
    with pytest.raises(UnsupportedFrameworkError):
        detect_framework(tmp_path)


# ── test paths ──

@pytest.fixture
def profile(notes_repo):
    return detect_framework(notes_repo)[1]


@pytest.mark.parametrize("path,problem", [
    ("tests/test_security_idor_note.py", None),
    ("tests/security/test_idor.py", None),
    ("", "no test file path"),
    ("../tests/test_x.py", "without '..'"),
    ("/tmp/test_x.py", "without '..'"),
    ("tests\\test_x.py", "forward slashes"),
    ("tests/test x.py", "may only contain"),
    ("tests/conftest.py", "not conftest.py"),
    ("tests/idor_checks.py", "would not be discovered"),
    ("tests/test_x.txt", "must be a .py file"),
    ("src/test_x.py", "existing test directory `tests/`"),
])
def test_validate_test_path(profile, path, problem):
    error = adapter.validate_test_path(profile, path)
    if problem is None:
        assert error is None
    else:
        assert problem in error


def test_suggested_path_never_collides(profile):
    invariant = IdorSpec().extract_invariant(verified_idor_finding())
    taken = {"tests/test_security_idor_note.py", "tests/test_security_idor_note_2.py"}
    assert adapter.suggest_test_path(profile, invariant, taken.__contains__) == "tests/test_security_idor_note_3.py"


# ── static policy ──

def test_well_formed_regression_test_passes_policy():
    assert adapter.static_check(GOOD_TEST) == []


@pytest.mark.parametrize("snippet,expected", [
    ("import requests\n", "makes real network requests"),
    ("from urllib.request import urlopen\n", "makes real network requests"),
    ("import subprocess\n", "spawns processes"),
    ("import socket\n", "raw network sockets"),
    ("import os\nos.system('id')\n", "runs shell commands"),
    ("import time\ntime.sleep(1)\n", "sleeps"),
    ("from time import sleep\n", "sleeps"),
    ("import pytest\npytest.skip('later')\n", "skips the test"),
    ("import pytest\n@pytest.mark.skip\ndef test_y():\n    assert False\n", "could pass without running"),
    ("import pytest\n@pytest.mark.xfail(strict=False)\ndef test_y():\n    assert False\n", "could pass without running"),
    ("from pytest import mark\n@mark.skipif(True, reason='x')\ndef test_y():\n    assert False\n", "could pass without running"),
    ("import unittest\n@unittest.skipIf(True, 'x')\ndef test_y():\n    assert False\n", "could pass without running"),
    ("def test_y():\n    try:\n        assert False\n    except AssertionError:\n        pass\n", "swallow the invariant check"),
    ("def test_y():\n    try:\n        assert False\n    except:\n        pass\n", "swallow the invariant check"),
    ("def pytest_runtest_makereport(item, call):\n    pass\n", "defines pytest hook"),
    ("import sys\nsys.exit(0)\n", "terminates the test process"),
    ("eval('1')\n", "evaluates dynamic code"),
    ("import atexit\n", "tamper with test results"),
    ("TOKEN = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl'\n", "embeds a JSON Web Token"),
])
def test_policy_violations(snippet, expected):
    code = GOOD_TEST + "\n" + snippet
    assert any(expected in v for v in adapter.static_check(code)), adapter.static_check(code)


def test_policy_requires_tests_and_assertions():
    assert "defines no test functions" in " ".join(adapter.static_check("x = 1\n"))
    assert "contains no assert statements" in " ".join(adapter.static_check("def test_a():\n    pass\n"))
    assert adapter.static_check("def test_a(:\n")[0].startswith("syntax error on line 1")


# ── results ──

def test_parse_results_distinguishes_assertions_crashes_errors_and_skips(tmp_path):
    (tmp_path / "junit.xml").write_text(junit_xml({
        "test_owner_can_read_own_note": "passed",
        "test_other_user_cannot_read_note": "failed",
        "test_other_user_cannot_modify_note": "crashed",
        "test_other_user_cannot_delete_note": "error",
        "test_param[read]": "skipped",
    }))
    (tmp_path / "output.txt").write_text("Authorization: Bearer abc.def.ghi\n4 tests ran\n")
    report = adapter.parse_results(tmp_path, 1, False)

    outcomes = {c.name: (c.outcome, c.assertion) for c in report.cases}
    assert outcomes == {
        "test_owner_can_read_own_note": ("passed", False),
        "test_other_user_cannot_read_note": ("failed", True),
        "test_other_user_cannot_modify_note": ("failed", False),
        "test_other_user_cannot_delete_note": ("error", False),
        "test_param[read]": ("skipped", False),
    }
    assert [c.base_name for c in report.cases][-1] == "test_param"
    assert report.collection_error is None
    assert "abc.def.ghi" not in report.output_tail


def test_parse_results_reports_collection_errors(tmp_path):
    (tmp_path / "junit.xml").write_text(COLLECTION_ERROR_XML)
    report = adapter.parse_results(tmp_path, 2, False)
    assert "No module named 'notes'" in report.collection_error
    assert report.cases == []


def test_parse_results_without_results_file(tmp_path):
    (tmp_path / "output.txt").write_text("ERROR: file or directory not found\n")
    report = adapter.parse_results(tmp_path, 4, False)
    assert "produced no results (exit code 4)" in report.collection_error
    assert "file or directory not found" in report.collection_error


def test_parse_results_rejects_entity_declarations(tmp_path):
    (tmp_path / "junit.xml").write_text(
        '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><testsuites><testsuite>'
        '<testcase classname="t" name="test_a"/></testsuite></testsuites>'
    )
    assert "DTD/entity" in adapter.parse_results(tmp_path, 0, False).collection_error


def test_parse_results_on_timeout(tmp_path):
    report = adapter.parse_results(tmp_path, None, True)
    assert report.timed_out and report.cases == []


# ── sandbox image + command ──

def test_dockerfile_installs_the_projects_own_test_dependencies(profile):
    dockerfile = adapter.dockerfile(profile, "python:3.12-slim")
    assert dockerfile.splitlines()[1] == "FROM python:3.12-slim"
    assert 'COPY ["requirements.txt", "/workspace/requirements.txt"]' in dockerfile
    assert 'RUN pip install --no-cache-dir -r "requirements.txt"' in dockerfile
    assert 'COPY [".", "/workspace"]' in dockerfile
    assert "chown -R 65534:65534 /workspace /openhack-results" in dockerfile
    assert adapter.default_base_image(profile) == "python:3.12-slim"


def test_dockerfile_for_subproject_with_includes(tmp_path):
    write_file(tmp_path, "api/requirements.txt", "flask\n")
    write_file(tmp_path, "api/requirements-dev.txt", "-r requirements.txt\n-c constraints/pins.txt\npytest\n")
    write_file(tmp_path, "api/constraints/pins.txt", "flask==3.0.3\n")
    write_file(tmp_path, "api/setup.py", "from setuptools import setup\nsetup(name='api')\n")
    write_file(tmp_path, "api/tests/test_a.py", "def test_a():\n    assert True\n")
    _, profile = detect_framework(tmp_path, "api/app.py")
    assert profile.project_root == "api"
    assert profile.dependency_files == ["requirements.txt", "requirements-dev.txt", "constraints/pins.txt"]
    dockerfile = adapter.dockerfile(profile, "python:3.11-slim")
    assert "WORKDIR /workspace/api" in dockerfile
    assert 'COPY ["api/constraints/pins.txt", "/workspace/api/constraints/pins.txt"]' in dockerfile
    assert 'pip install --no-cache-dir -e "."' in dockerfile
    assert adapter.display_command(profile, "tests/test_x.py") == "cd api && python -m pytest tests/test_x.py"


def test_container_command_runs_one_file_with_junit_results(profile):
    command = adapter.container_command(profile, "tests/test_security_idor_note.py")
    assert command[:2] == ["sh", "-c"]
    assert command[-1] == "tests/test_security_idor_note.py"
    assert "--junitxml=/openhack-results/junit.xml" in command[2]
    assert "--maxfail=100000" in command[2]
    assert "> /openhack-results/output.txt 2>&1" in command[2]
