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
- bounded: memory, CPU and PID limits plus a wall-clock timeout, after which
  the container is killed;
- disposable: every run gets a fresh container that is always removed.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from uuid import uuid4

from .orchestrator import SandboxError, SandboxOrchestrator, SandboxTimeoutError

logger = logging.getLogger(__name__)

LABEL = "openhack.regression=1"
UNPRIVILEGED_USER = "65534:65534"


@dataclass
class ContainerLimits:
    run_timeout: int = 180
    build_timeout: int = 900
    memory: str = "1g"
    cpus: str = "1"
    pids: int = 256


@dataclass
class ContainerRunResult:
    exit_code: Optional[int]
    timed_out: bool
    results_dir: Path
    stderr: str = ""
    container: str = field(default="", repr=False)

    def cleanup(self) -> None:
        shutil.rmtree(self.results_dir, ignore_errors=True)


async def _run(cmd: list[str], timeout: int, check: bool = True):
    try:
        return await SandboxOrchestrator._run_command(cmd, timeout=timeout, check=check)
    except FileNotFoundError as exc:
        raise SandboxError("the Docker CLI was not found on PATH") from exc


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
        start; ``results_path`` is copied back out afterwards. The container is
        removed whatever happens.
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
            stderr = ""
            try:
                proc = await _run(
                    ["docker", "start", "-a", name],
                    timeout=self.limits.run_timeout, check=False,
                )
                exit_code = proc.returncode
                stderr = (proc.stderr or "")[-2000:]
            except SandboxTimeoutError:
                timed_out = True
                await self._quiet(["docker", "kill", name])
            await self._quiet(["docker", "cp", f"{name}:{results_path}/.", str(results_dir)])
            return ContainerRunResult(exit_code, timed_out, results_dir, stderr, name)
        except BaseException:
            shutil.rmtree(results_dir, ignore_errors=True)
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            if created:
                await self._quiet(["docker", "rm", "-f", name])

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
