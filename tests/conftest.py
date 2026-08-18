import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from openhack.tools.filesystem import FileSystemTools
from openhack.tools.registry import ToolRegistry


def shell_command(source: str) -> str:
    """Return a shell-safe command that runs source with this Python."""
    args = [sys.executable, "-c", source]
    return subprocess.list2cmdline(args) if os.name == "nt" else shlex.join(args)


@pytest.fixture(autouse=True)
def _portable_prompt_toolkit_session():
    """Give non-console Windows test runs a deterministic terminal backend."""
    if os.name != "nt":
        yield
        return

    from prompt_toolkit.application.current import create_app_session
    from prompt_toolkit.input import DummyInput
    from prompt_toolkit.output import DummyOutput

    with create_app_session(input=DummyInput(), output=DummyOutput()):
        yield


@pytest.fixture(autouse=True)
def _stub_api_key(monkeypatch):
    """Give every test a dummy OpenHack key.

    Constructing an LLMClient raises without one, so any test that builds an
    agent fails on a machine with no credentials — which is exactly what CI is.
    Tests never make real API calls (they stub the client), so a placeholder is
    all that's needed, and forcing it also stops a developer's real key from
    leaking into a test run.

    The env var covers Settings objects built later (`reload_settings()` reads
    the environment). Every *already-live* Settings instance is patched too:
    modules do `from openhack.config import settings`, so once any test triggers
    a reload, those modules keep a stale object that a reload never updates.
    """
    import sys

    from openhack import config

    monkeypatch.setenv("OPENHACK_API_KEY", "sk-test-ci")
    seen = set()
    for module in list(sys.modules.values()):
        obj = getattr(module, "settings", None)
        if isinstance(obj, config.Settings) and id(obj) not in seen:
            seen.add(id(obj))
            monkeypatch.setattr(obj, "openhack_api_key", "sk-test-ci", raising=False)


@pytest.fixture
def fs_tools(tmp_path):
    return FileSystemTools(jail_dir=tmp_path)


@pytest.fixture
def tool_registry(tmp_path):
    return ToolRegistry(target_dir=tmp_path)


def write_file(base: Path, rel_path: str, content: str) -> Path:
    p = base / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


def write_json(base: Path, rel_path: str, data: dict) -> Path:
    return write_file(base, rel_path, json.dumps(data, indent=2))
