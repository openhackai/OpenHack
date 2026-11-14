"""
Locked-down container execution for generated test code.

Generated tests, and the repository code they exercise, are untrusted. They
only ever execute inside a Docker container started by this runner — never
on the host:

- no network (``--network none``): the image build may fetch the project's
  dependencies, but the test run itself cannot reach anything;
- no host mounts: code goes in through the image and ``docker cp``, results
  come back out the same way, so the container cannot write to the host;
- unprivileged: non-root user, every capability dropped, no-new-privileges;
- bounded: memory, CPU, PID and file-size limits plus a wall-clock timeout,
  after which the container is killed. The container is never attached and
  keeps no logs, and results come back as a size-capped stream of regular
  files, so nothing it writes can flood the host;
- disposable: every run gets a fresh container that is always removed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import IO, Optional
from uuid import uuid4

from .orchestrator import SandboxError, SandboxOrchestrator, SandboxTimeoutError

logger = logging.getLogger(__name__)

LABEL = "openhack.regression=1"
UNPRIVILEGED_USER = "65534:65534"
COPY_TIMEOUT = 60


@dataclass
class ContainerLimits:
    run_timeout: int = 180
    build_timeout: int = 900
    memory: str = "1g"
    cpus: str = "1"
    pids: int = 256
    # Largest file a test run may write, and most bytes of results copied
    # back to the host.
    file_size: int = 64 * 1024 * 1024
    results_bytes: int = 16 * 1024 * 1024


@dataclass
class ContainerRunResult:
    exit_code: Optional[int]
    timed_out: bool
    results_dir: Path
    # Why the results were discarded instead of copied back, if they were.
    results_error: Optional[str] = None
    container: str = field(default="", repr=False)

    def cleanup(self) -> None:
        shutil.rmtree(self.results_dir, ignore_errors=True)


async def _run(cmd: list[str], timeout: int, check: bool = True):
    try:
        return await SandboxOrchestrator._run_command(cmd, timeout=timeout, check=check)
    except FileNotFoundError as exc:
        raise SandboxError("the Docker CLI was not found on PATH") from exc


def _extract_regular_files(archive: IO[bytes], dest: Path) -> Optional[str]:
    """Write the regular files of a results archive under ``dest``.

    Links, devices and directories are never created, and no path may leave
    ``dest``. Returns why the archive could not be read, or None.
    """
    try:
        with tarfile.open(fileobj=archive, mode="r:") as tar:
            for member in tar:
                relpath = PurePosixPath(member.name)
                target = dest.joinpath(*relpath.parts)
                if (not member.isreg() or not relpath.parts or relpath.is_absolute()
                        or ".." in relpath.parts or not target.is_relative_to(dest)):
                    continue
                source = tar.extractfile(member)
                if source is None:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with source, open(target, "wb") as out:
                    shutil.copyfileobj(source, out)
    except (tarfile.TarError, OSError) as exc:
        return f"could not read the test results: {exc}"
    return None


class ContainerTestRunner:
    """Builds per-revision images and runs one test file per fresh container."""

    def __init__(self, limits: Optional[ContainerLimits] = None):
        self.limits = limits or ContainerLimits()
        self._images: set[str] = set()

    def lockdown_flags(self) -> list[str]:
        limits = self.limits
        return [
            "--network", "none",
            "--memory", limits.memory,
            "--memory-swap", limits.memory,
            "--cpus", str(limits.cpus),
            "--pids-limit", str(limits.pids),
            "--ulimit", f"fsize={limits.file_size}:{limits.file_size}",
            "--ulimit", "core=0",
            "--log-driver", "none",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--user", UNPRIVILEGED_USER,
            "--env", "HOME=/tmp",
            "--env", "PYTHONDONTWRITEBYTECODE=1",
        ]

    async def ensure_available(self) -> None:
        try:
            await _run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=30)
        except SandboxError as exc:
            raise SandboxError(
                f"Docker is required to validate regression tests but is not available: {exc}"
            ) from exc

    async def build_image(self, context_dir: Path, dockerfile: str, tag: str) -> None:
        dockerfile_path = context_dir / ".openhack-regression.Dockerfile"
        dockerfile_path.write_text(dockerfile, encoding="utf-8")
        cmd = [
            "docker", "build", "--label", LABEL, "-t", tag,
            "-f", str(dockerfile_path), str(context_dir),
        ]
        try:
            await _run(cmd, timeout=self.limits.build_timeout)
        except SandboxTimeoutError as exc:
            raise SandboxError(
                f"sandbox image build timed out after {self.limits.build_timeout}s"
            ) from exc
        except SandboxError as exc:
            detail = str(exc)
            raise SandboxError(
                "sandbox image build failed (could the project's test dependencies "
                f"be installed?): {detail[-1500:]}"
            ) from exc
        self._images.add(tag)

    async def run_test(
        self,
        image: str,
        files: dict[str, str],
        command: list[str],
        workdir: str = "/workspace",
        results_path: str = "/openhack-results",
    ) -> ContainerRunResult:
        """Run ``command`` in a fresh locked-down container of ``image``.

        ``files`` (path relative to /workspace -> content) are copied in before
        start; the regular files under ``results_path`` are copied back out
        afterwards, up to ``results_bytes``. The container is removed whatever
        happens.
        """
        name = f"openhack-regression-{uuid4().hex[:12]}"
        results_dir = Path(tempfile.mkdtemp(prefix="openhack-results-"))
        staging = Path(tempfile.mkdtemp(prefix="openhack-stage-"))
        created = False
        try:
            await _run(
                ["docker", "create", "--name", name, "--label", LABEL,
                 *self.lockdown_flags(), "--workdir", workdir, image, *command],
                timeout=60,
            )
            created = True
            for relpath, content in files.items():
                target = (staging / relpath).resolve()
                if not target.is_relative_to(staging.resolve()):
                    raise SandboxError(f"refusing to stage a file outside the workspace: {relpath}")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                os.chmod(target, 0o644)
            await _run(["docker", "cp", f"{staging}{os.sep}.", f"{name}:/workspace"], timeout=60)

            exit_code: Optional[int] = None
            timed_out = False
            # Not attached, so nothing the test prints is buffered on the host.
            await _run(["docker", "start", name], timeout=60)
            try:
                waited = await _run(["docker", "wait", name], timeout=self.limits.run_timeout)
            except SandboxTimeoutError:
                timed_out = True
                await self._quiet(["docker", "kill", name])
            else:
                try:
                    exit_code = int(waited.stdout.split()[-1])
                except (IndexError, ValueError) as exc:
                    raise SandboxError(f"unexpected `docker wait` output: {waited.stdout[:200]!r}") from exc
            results_error = await self._copy_results(name, results_path, results_dir)
            return ContainerRunResult(exit_code, timed_out, results_dir, results_error, name)
        except BaseException:
            shutil.rmtree(results_dir, ignore_errors=True)
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            if created:
                await self._quiet(["docker", "rm", "-f", name])

    async def _copy_results(self, name: str, results_path: str, dest: Path) -> Optional[str]:
        """Copy the regular files under ``results_path`` into ``dest``.

        The directory arrives as a tar stream that is cut off past
        ``results_bytes``, so a test cannot fill the host's disk, and only
        regular files are written: a symlink planted in the results would
        otherwise make the host read one of its own files. Returns why the
        results were discarded, or None.
        """
        limit = self.limits.results_bytes
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "cp", f"{name}:{results_path}/.", "-",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            raise SandboxError("the Docker CLI was not found on PATH") from exc
        stream = proc.stdout
        assert stream is not None

        async def receive(archive: IO[bytes]) -> bool:
            received = 0
            while chunk := await stream.read(1 << 16):
                received += len(chunk)
                if received <= limit:
                    archive.write(chunk)
                elif received - len(chunk) <= limit and proc.returncode is None:
                    # Over the limit: stop the copy and discard what it already
                    # wrote. Reading on to EOF matters: before Python 3.12,
                    # wait() only returns once the pipe is drained.
                    proc.kill()
            await proc.wait()
            return received <= limit

        with tempfile.TemporaryFile() as archive:
            complete: Optional[bool] = None
            try:
                complete = await asyncio.wait_for(receive(archive), timeout=COPY_TIMEOUT)
            except asyncio.TimeoutError as exc:
                raise SandboxError(f"copying the test results timed out after {COPY_TIMEOUT}s") from exc
            finally:
                if complete is None and proc.returncode is None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
            if not complete:
                return (
                    f"the test run left more than {limit // (1024 * 1024)} MB in its results "
                    "directory, so the results were discarded; keep test output small"
                )
            if proc.returncode != 0:
                # No results directory to copy; the adapter reports missing results.
                logger.debug(f"docker cp of {results_path} from {name} exited {proc.returncode}")
                return None
            archive.seek(0)
            return _extract_regular_files(archive, dest)

    async def remove_images(self) -> None:
        for tag in list(self._images):
            await self._quiet(["docker", "rmi", "-f", tag])
            self._images.discard(tag)

    @staticmethod
    async def _quiet(cmd: list[str]) -> None:
        try:
            await _run(cmd, timeout=60, check=False)
        except (SandboxError, OSError) as exc:
            logger.debug(f"sandbox cleanup command failed ({' '.join(cmd)}): {exc}")
