"""write_file — the tool whose absence caused the heredoc retry loop.

Session cd7d02b8 burned ~4 minutes and ~1M tokens because the only way to
create an exploit PoC was a `cat > f << 'EOF'` heredoc, whose body has to fit
inside the tool-call JSON. It truncated, json.loads failed, run_command landed
with empty args, and the model retried the identical doomed command.
"""

from pathlib import Path

import pytest

from openhack.tools.filewrite import FileWriteTools
from openhack.tools.registry import ToolRegistry


def test_writes_a_file(tmp_path):
    w = FileWriteTools(jail_dir=tmp_path)
    out = w.write_file("exploit.py", "#!/usr/bin/env python3\nprint('pwn')\n")
    assert out["path"] == "exploit.py"
    assert (tmp_path / "exploit.py").read_text().endswith("print('pwn')\n")
    assert out["bytes_written"] > 0


def test_creates_parent_directories(tmp_path):
    w = FileWriteTools(jail_dir=tmp_path)
    w.write_file("poc/wp2shell/run.py", "x = 1\n")
    assert (tmp_path / "poc" / "wp2shell" / "run.py").exists()


def test_append_builds_a_large_file_in_chunks(tmp_path):
    # The actual remedy for the truncation loop: several bounded calls.
    w = FileWriteTools(jail_dir=tmp_path)
    w.write_file("big.py", "part1\n")
    w.write_file("big.py", "part2\n", append=True)
    out = w.write_file("big.py", "part3\n", append=True)
    assert (tmp_path / "big.py").read_text() == "part1\npart2\npart3\n"
    assert out["appended"] is True


def test_overwrite_replaces_content(tmp_path):
    w = FileWriteTools(jail_dir=tmp_path)
    w.write_file("f.txt", "old")
    w.write_file("f.txt", "new")
    assert (tmp_path / "f.txt").read_text() == "new"


def test_mode_makes_a_script_executable(tmp_path):
    w = FileWriteTools(jail_dir=tmp_path)
    w.write_file("run.sh", "#!/bin/sh\necho hi\n", mode="755")
    assert (tmp_path / "run.sh").stat().st_mode & 0o111


def test_jailed_to_the_session_root(tmp_path):
    w = FileWriteTools(jail_dir=tmp_path)
    out = w.write_file("../escaped.txt", "nope")
    assert "error" in out and "outside" in out["error"].lower()
    assert not (tmp_path.parent / "escaped.txt").exists()


def test_oversized_content_is_refused_with_guidance(tmp_path):
    w = FileWriteTools(jail_dir=tmp_path)
    out = w.write_file("big.py", "x" * (FileWriteTools.MAX_CONTENT + 1))
    assert out["error"] == "content_too_large"
    assert "append=true" in out["note"]


def test_missing_path_is_an_error(tmp_path):
    assert FileWriteTools(jail_dir=tmp_path).write_file("", "x")["error"] == "missing_path"


# ------------------------------------------------------------------- wiring

def test_exposed_to_agents_but_not_the_scan_pipeline(tmp_path):
    agent = {t["name"] for t in
             ToolRegistry(target_dir=tmp_path, include_agent_tools=True).get_all_tool_definitions()}
    scan = {t["name"] for t in ToolRegistry(target_dir=tmp_path).get_all_tool_definitions()}
    assert "write_file" in agent
    assert "write_file" not in scan, "the scan pipeline must stay read-only"


def test_registry_dispatches_write_file(tmp_path):
    reg = ToolRegistry(target_dir=tmp_path, include_agent_tools=True)
    reg.execute_tool("write_file", {"path": "a.txt", "content": "hello"})
    assert (tmp_path / "a.txt").read_text() == "hello"


def test_prompt_tells_the_agent_to_use_it_over_heredocs():
    from openhack.agents.interactive import SYSTEM_PROMPT
    assert "write_file" in SYSTEM_PROMPT
    assert "heredoc" in SYSTEM_PROMPT.lower()
