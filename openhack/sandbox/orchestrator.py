"""
Docker sandbox orchestrator.

Manages the lifecycle of target applications in isolated Docker containers:
- Detects docker-compose.yml / Dockerfile in the target repo
- Builds and starts the application
- Waits for health check
- Provides the base URL for exploit execution
- Tears down containers on completion
"""

import asyncio
import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class SandboxConfig:
    """Configuration for the sandbox environment."""
    # How to start the app
    build_command: Optional[str] = None       # e.g. "docker-compose up --build -d"
    compose_file: Optional[str] = None        # path to docker-compose.yml relative to target
    dockerfile: Optional[str] = None          # path to Dockerfile relative to target

    # Health check
    health_check_url: Optional[str] = None    # e.g. "http://localhost:3000/api/health"
    health_check_port: int = 3000
    health_check_path: str = "/"
    health_check_timeout: int = 120           # seconds to wait for app to be ready

    # Environment
    env_vars: dict[str, str] = field(default_factory=dict)

    # Network isolation
    network_mode: str = "bridge"              # "bridge" or "none" for full isolation
    host_port: int = 0                        # 0 = auto-assign

    # Cleanup
    teardown_on_complete: bool = True


@dataclass
class SandboxStatus:
    """Current status of the sandbox."""
    running: bool = False
    base_url: str = ""
    container_ids: list[str] = field(default_factory=list)
    project_name: str = ""
    start_time: float = 0.0
    host_port: int = 0


class SandboxOrchestrator:
    """Manages Docker sandbox lifecycle for exploit verification."""

    def __init__(self, target_dir: Path, config: Optional[SandboxConfig] = None):
        self.target_dir = target_dir.resolve()
        self.config = config or SandboxConfig()
        self.status = SandboxStatus()
        self._project_name = f"openhack-sandbox-{int(time.time())}"

    async def start(self) -> SandboxStatus:
        """Start the target application in a Docker sandbox."""
        logger.info(f"Starting sandbox for {self.target_dir}")

        # Auto-detect how to start the app
        compose_file = self._find_compose_file()
        dockerfile = self._find_dockerfile()

        if not compose_file and not dockerfile:
            raise SandboxError(
                f"No docker-compose.yml or Dockerfile found in {self.target_dir}. "
                "Cannot start sandbox without containerization config."
            )

        # Assign a host port
        host_port = self.config.host_port or await self._find_free_port()
        self.status.host_port = host_port

        try:
            if compose_file:
                await self._start_with_compose(compose_file, host_port)
            else:
                await self._start_with_dockerfile(dockerfile, host_port)
        except SandboxError:
            await self._force_cleanup_network()
            raise

        # Wait for health check
        base_url = f"http://localhost:{host_port}"
        self.status.base_url = base_url

        health_url = self.config.health_check_url or f"{base_url}{self.config.health_check_path}"
        try:
            await self._wait_for_health(health_url)
        except SandboxError:
            self.status.running = True
            await self.stop()
            raise

        self.status.running = True
        self.status.start_time = time.time()
        logger.info(f"Sandbox ready at {base_url}")
        return self.status

    async def stop(self) -> None:
        """Tear down the sandbox containers and clean up Docker resources."""
        if not self.status.running:
            return

        logger.info(f"Stopping sandbox {self._project_name}")
        try:
            compose_file = self._find_compose_file()
            if compose_file:
                cmd = [
                    "docker", "compose", "-p", self._project_name,
                    "-f", str(compose_file),
                ]
                override = getattr(self, '_override_file', None)
                if override and override.exists():
                    cmd.extend(["-f", str(override)])
                cmd.extend(["down", "-v", "--remove-orphans"])

                await self._run_command(cmd, timeout=30)

                # Clean up override file
                if override and override.exists():
                    override.unlink()
            else:
                for cid in self.status.container_ids:
                    await self._run_command(
                        ["docker", "rm", "-f", cid], timeout=15,
                    )
        except Exception as e:
            logger.warning(f"Error during sandbox teardown: {e}")
            await self._force_cleanup_network()
        finally:
            self.status.running = False
            self.status.container_ids = []
            logger.info("Sandbox stopped")

    async def _force_cleanup_network(self) -> None:
        """Remove the Docker network for this project if it still exists."""
        network_name = f"{self._project_name}_default"
        try:
            await self._run_command(
                ["docker", "network", "rm", network_name], timeout=10,
            )
            logger.info(f"Cleaned up stale network: {network_name}")
        except Exception:
            pass

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.config.teardown_on_complete:
            await self.stop()

    def _find_compose_file(self) -> Optional[Path]:
        """Find docker-compose file in the target directory."""
        if self.config.compose_file:
            path = self.target_dir / self.config.compose_file
            return path if path.exists() else None

        for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
            path = self.target_dir / name
            if path.exists():
                return path
        return None

    def _find_dockerfile(self) -> Optional[Path]:
        """Find Dockerfile in the target directory."""
        if self.config.dockerfile:
            path = self.target_dir / self.config.dockerfile
            return path if path.exists() else None

        path = self.target_dir / "Dockerfile"
        return path if path.exists() else None

    async def _find_free_port(self) -> int:
        """Find a free port on the host."""
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("", 0))
            return s.getsockname()[1]

    async def _start_with_compose(self, compose_file: Path, host_port: int) -> None:
        """Start the app using docker-compose.

        Generates an override file that:
        - Removes hardcoded container_name directives (project name handles naming)
        - Remaps the app port to an available host port
        - Remaps any other ports that might conflict
        """
        self.status.project_name = self._project_name
        self._override_file: Optional[Path] = None

        # Parse compose file to find the app service and generate overrides
        override = await self._generate_compose_override(compose_file, host_port)

        env = {**self.config.env_vars, "HOST_PORT": str(host_port)}

        # Write override file
        override_path = self.target_dir / f".openhack-sandbox-override-{self._project_name}.yml"
        override_path.write_text(override, encoding="utf-8")
        self._override_file = override_path

        # Build and start with override
        cmd = [
            "docker", "compose",
            "-p", self._project_name,
            "-f", str(compose_file),
            "-f", str(override_path),
            "up", "--build", "-d",
        ]

        logger.info(f"Starting with compose (port {host_port}): {' '.join(cmd)}")
        await self._run_command(cmd, cwd=self.target_dir, env=env, timeout=300)

        # Get container IDs
        ps_cmd = [
            "docker", "compose", "-p", self._project_name,
            "-f", str(compose_file),
            "-f", str(override_path),
            "ps", "-q",
        ]
        result = await self._run_command(ps_cmd, cwd=self.target_dir, timeout=10)
        self.status.container_ids = [
            cid.strip() for cid in result.stdout.strip().split("\n") if cid.strip()
        ]

    async def _generate_compose_override(self, compose_file: Path, host_port: int) -> str:
        """Generate a docker-compose override that remaps ports and removes container names.

        Parses the compose file with a simple line-based approach to avoid
        requiring pyyaml as a dependency.
        """
        compose_text = compose_file.read_text(encoding="utf-8")

        # Extract service names and their port mappings
        services = self._parse_compose_services(compose_text)

        override: dict = {"services": {}}

        for svc_name, svc_info in services.items():
            svc_override: dict = {}

            if svc_info.get("container_name"):
                svc_override["container_name"] = f"{self._project_name}-{svc_name}"

            ports = svc_info.get("ports", [])
            if ports:
                new_ports = []
                for port_mapping in ports:
                    if ":" in port_mapping:
                        parts = port_mapping.strip('"').strip("'").split(":")
                        container_port = parts[-1]
                        if container_port == str(self.config.health_check_port):
                            new_ports.append(f"{host_port}:{container_port}")
                        else:
                            free = await self._find_free_port()
                            new_ports.append(f"{free}:{container_port}")
                    else:
                        new_ports.append(port_mapping)
                svc_override["ports"] = new_ports

            if svc_override:
                override["services"][svc_name] = svc_override

        return self._dict_to_yaml(override, override_lists=True)

    @staticmethod
    def _parse_compose_services(text: str) -> dict:
        """Minimally parse a docker-compose file to extract service info.

        Returns {service_name: {"container_name": str|None, "ports": [str]}}.
        """
        services = {}
        current_service = None
        in_services = False
        in_ports = False
        services_indent = None
        service_indent = None
        prop_indent = None

        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue

            indent = len(line) - len(line.lstrip())

            # Find the top-level "services:" key
            if stripped == "services:" and indent == 0:
                in_services = True
                services_indent = indent
                continue

            # Exited services block
            if in_services and indent == 0 and stripped and not stripped.startswith("-"):
                in_services = False
                current_service = None
                continue

            if not in_services:
                continue

            # Service name: first level of indent under services (typically 2 spaces)
            if stripped.endswith(":") and ":" not in stripped[:-1]:
                if service_indent is None or indent <= service_indent:
                    name = stripped.rstrip(":")
                    current_service = name
                    service_indent = indent
                    services[current_service] = {"container_name": None, "ports": []}
                    in_ports = False
                    prop_indent = None
                    continue

            if current_service is None:
                continue

            # Service properties are one level deeper than service name
            if prop_indent is None and indent > service_indent:
                prop_indent = indent

            # If we're back to service-level indent, it's a new service
            if indent <= service_indent and stripped.endswith(":") and ":" not in stripped[:-1]:
                name = stripped.rstrip(":")
                current_service = name
                services[current_service] = {"container_name": None, "ports": []}
                in_ports = False
                prop_indent = None
                continue

            if prop_indent is not None and indent == prop_indent:
                in_ports = False
                if stripped.startswith("container_name:"):
                    val = stripped.split(":", 1)[1].strip().strip('"').strip("'")
                    services[current_service]["container_name"] = val
                elif stripped == "ports:":
                    in_ports = True

            elif in_ports and stripped.startswith("-"):
                port = stripped.lstrip("- ").strip('"').strip("'")
                services[current_service]["ports"].append(port)

        return services

    @staticmethod
    def _dict_to_yaml(d: dict, indent: int = 0, override_lists: bool = False) -> str:
        """Minimal dict-to-YAML serializer (avoids pyyaml dependency).

        When override_lists=True, list keys emit `!override` so Docker Compose
        replaces the base list instead of merging into it.
        """
        lines = []
        prefix = "  " * indent
        for key, val in d.items():
            if isinstance(val, dict):
                lines.append(f"{prefix}{key}:")
                lines.append(SandboxOrchestrator._dict_to_yaml(val, indent + 1, override_lists))
            elif isinstance(val, list):
                tag = " !override" if override_lists else ""
                lines.append(f"{prefix}{key}:{tag}")
                for item in val:
                    if isinstance(item, dict):
                        items = list(item.items())
                        first_key, first_val = items[0]
                        lines.append(f"{prefix}  - {first_key}: {first_val}")
                        for k, v in items[1:]:
                            lines.append(f"{prefix}    {k}: {v}")
                    else:
                        lines.append(f"{prefix}  - \"{item}\"" if isinstance(item, str) else f"{prefix}  - {item}")
            else:
                if isinstance(val, str):
                    lines.append(f"{prefix}{key}: \"{val}\"")
                else:
                    lines.append(f"{prefix}{key}: {val}")
        return "\n".join(lines)

    async def _start_with_dockerfile(self, dockerfile: Path, host_port: int) -> None:
        """Start the app by building a Dockerfile and running the container."""
        image_name = f"openhack-sandbox:{self._project_name}"

        # Build
        build_cmd = [
            "docker", "build",
            "-t", image_name,
            "-f", str(dockerfile),
            str(self.target_dir),
        ]
        logger.info(f"Building image: {image_name}")
        await self._run_command(build_cmd, timeout=300)

        # Run
        app_port = self.config.health_check_port
        run_cmd = [
            "docker", "run", "-d",
            "--name", self._project_name,
            "-p", f"{host_port}:{app_port}",
        ]

        for k, v in self.config.env_vars.items():
            run_cmd.extend(["-e", f"{k}={v}"])

        run_cmd.append(image_name)

        logger.info(f"Running container: {self._project_name}")
        result = await self._run_command(run_cmd, timeout=30)
        container_id = result.stdout.strip()
        self.status.container_ids = [container_id]

    async def _wait_for_health(self, health_url: str) -> None:
        """Poll the health check URL until the app is ready."""
        import aiohttp

        timeout = self.config.health_check_timeout
        start = time.time()
        last_error = None

        logger.info(f"Waiting for health check: {health_url} (timeout: {timeout}s)")

        while time.time() - start < timeout:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(health_url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        if resp.status < 500:
                            elapsed = time.time() - start
                            logger.info(f"Health check passed (status {resp.status}) in {elapsed:.1f}s")
                            return
                        last_error = f"HTTP {resp.status}"
            except Exception as e:
                last_error = str(e)

            await asyncio.sleep(2)

        # Pull the last few lines of container logs so the error surfaces
        # *why* the app failed (Prisma schema missing, port conflict, etc.)
        # instead of just "HTTP 500".
        tail_logs = ""
        try:
            tail_logs = await self.get_logs(tail=40)
        except Exception:
            pass
        log_snippet = (
            "\n— Recent container logs —\n" + tail_logs.strip()[-2000:]
            if tail_logs.strip() else ""
        )
        raise SandboxError(
            f"Health check failed after {timeout}s. "
            f"URL: {health_url}, last error: {last_error}{log_snippet}"
        )

    async def get_logs(self, tail: int = 100) -> str:
        """Get container logs for debugging."""
        logs = []
        for cid in self.status.container_ids:
            try:
                result = await self._run_command(
                    ["docker", "logs", "--tail", str(tail), cid], timeout=10,
                )
                logs.append(f"=== Container {cid[:12]} ===\n{result.stdout}")
                if result.stderr:
                    logs.append(f"STDERR:\n{result.stderr}")
            except Exception as e:
                logs.append(f"=== Container {cid[:12]} === ERROR: {e}")
        return "\n".join(logs)

    @staticmethod
    async def _run_command(
        cmd: list[str],
        cwd: Optional[Path] = None,
        env: Optional[dict] = None,
        timeout: int = 60,
    ) -> asyncio.subprocess.Process:
        """Run a shell command asynchronously."""
        import os
        full_env = {**os.environ, **(env or {})}

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=full_env,
        )

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise SandboxError(f"Command timed out after {timeout}s: {' '.join(cmd)}")

        proc.stdout = stdout.decode() if stdout else ""
        proc.stderr = stderr.decode() if stderr else ""

        if proc.returncode != 0:
            raise SandboxError(
                f"Command failed (exit {proc.returncode}): {' '.join(cmd)}\n"
                f"STDERR: {proc.stderr[:2000]}"
            )

        return proc


class SandboxError(Exception):
    """Raised when sandbox operations fail."""
    pass
