"""The locked-down container runner, without a Docker daemon.

`docker` is replaced at the process boundary: a stand-in `docker cp` streams a
crafted tar archive, so the size cap and the regular-files-only extraction run
for real. test_regression_e2e_docker.py covers the same paths against Docker.
"""

import asyncio
import io
import sys
import tarfile
from types import SimpleNamespace

import pytest

from openhack.sandbox import container_runner
from openhack.sandbox.container_runner import ContainerLimits, ContainerTestRunner
from openhack.sandbox.orchestrator import SandboxTimeoutError

MB = 1024 * 1024


def _archive(*members) -> bytes:
    """A tar archive from (name, type, payload) members."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, kind, payload in members:
            info = tarfile.TarInfo(name)
            if kind == "file":
                data = payload if isinstance(payload, bytes) else payload.encode()
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
                continue
            info.type = {"dir": tarfile.DIRTYPE, "symlink": tarfile.SYMTYPE, "hardlink": tarfile.LNKTYPE}[kind]
            info.linkname = payload or ""
            tar.addfile(info)
    return buffer.getvalue()


def _copy_results(monkeypatch, tmp_path, stream: bytes = b"", exit_code: int = 0, limits=None):
    """Run _copy_results against a stand-in `docker cp` that writes ``stream``."""
    payload = tmp_path / "stream.bin"
    payload.write_bytes(stream)
    real_exec = asyncio.create_subprocess_exec
    seen = []

    async def docker_cp(*cmd, **kwargs):
        seen.append(cmd)
        script = ("import shutil, sys; shutil.copyfileobj(open(sys.argv[1], 'rb'), sys.stdout.buffer); "
                  "sys.exit(int(sys.argv[2]))")
        return await real_exec(sys.executable, "-c", script, str(payload), str(exit_code), **kwargs)

    monkeypatch.setattr(container_runner.asyncio, "create_subprocess_exec", docker_cp)
    dest = tmp_path / "results"
    dest.mkdir()
    error = asyncio.run(ContainerTestRunner(limits)._copy_results("openhack-regression-x", "/openhack-results", dest))
    assert seen == [("docker", "cp", "openhack-regression-x:/openhack-results/.", "-")]
    return error, dest


def _written(dest):
    return sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if not p.is_dir())


def test_only_regular_files_come_back(monkeypatch, tmp_path):
    stream = _archive(
        ("./", "dir", None),
        ("./junit.xml", "file", "<testsuites/>"),
        # A planted link would make the host read its own file when results are parsed.
        ("./output.txt", "symlink", "/etc/hostname"),
        ("./hosts", "hardlink", "/etc/hosts"),
        ("./logs/run.txt", "file", "nested output"),
        ("../escape.txt", "file", "outside"),
    )
    error, dest = _copy_results(monkeypatch, tmp_path, stream)

    assert error is None
    assert _written(dest) == ["junit.xml", "logs/run.txt"]
    assert (dest / "junit.xml").read_text() == "<testsuites/>"
    assert not (tmp_path / "escape.txt").exists()


def test_results_beyond_the_limit_are_discarded(monkeypatch, tmp_path):
    stream = _archive(("./junit.xml", "file", "<testsuites/>"), ("./output.txt", "file", b"x" * (2 * MB)))
    error, dest = _copy_results(monkeypatch, tmp_path, stream, limits=ContainerLimits(results_bytes=MB))

    assert "more than 1 MB in its results directory" in error
    assert _written(dest) == []


def test_missing_results_directory_leaves_nothing(monkeypatch, tmp_path):
    error, dest = _copy_results(monkeypatch, tmp_path, b"", exit_code=1)
    assert error is None and _written(dest) == []


def test_unreadable_results_archive(monkeypatch, tmp_path):
    error, dest = _copy_results(monkeypatch, tmp_path, b"this is not a tar archive" * 40)
    assert error.startswith("could not read the test results") and _written(dest) == []


def test_lockdown_flags_bound_files_and_keep_no_logs():
    flags = " ".join(ContainerTestRunner(ContainerLimits(file_size=4 * MB)).lockdown_flags())
    assert f"--ulimit fsize={4 * MB}:{4 * MB}" in flags
    assert "--ulimit core=0" in flags and "--log-driver none" in flags
    assert "--network none" in flags and "--cap-drop ALL" in flags


@pytest.fixture
def docker_calls(monkeypatch):
    """Record docker commands; ``wait`` reports exit 3 unless told to time out."""
    calls, state = [], {"wait_times_out": False}

    async def run(cmd, timeout, check=True):
        calls.append(cmd)
        if cmd[1] == "wait" and state["wait_times_out"]:
            raise SandboxTimeoutError("timed out")
        return SimpleNamespace(stdout="3\n" if cmd[1] == "wait" else "", stderr="", returncode=0)

    async def no_results(self, name, results_path, dest):
        return None

    monkeypatch.setattr(container_runner, "_run", run)
    monkeypatch.setattr(ContainerTestRunner, "_copy_results", no_results)
    return SimpleNamespace(calls=calls, state=state)


def test_containers_are_waited_on_not_attached(docker_calls):
    run = asyncio.run(ContainerTestRunner().run_test("image", {"tests/test_x.py": "x"}, ["true"]))
    run.cleanup()

    assert run.exit_code == 3 and not run.timed_out
    assert [cmd[1] for cmd in docker_calls.calls] == ["create", "cp", "start", "wait", "rm"]
    assert ["docker", "start", run.container] in docker_calls.calls


def test_timed_out_containers_are_killed_and_removed(docker_calls):
    docker_calls.state["wait_times_out"] = True
    run = asyncio.run(ContainerTestRunner().run_test("image", {}, ["sleep", "300"]))
    run.cleanup()

    assert run.timed_out and run.exit_code is None
    assert [cmd[1] for cmd in docker_calls.calls] == ["create", "cp", "start", "wait", "kill", "rm"]
