"""
Test-framework adapters for generated regression tests.

An adapter knows how a repository's *own* test suite is laid out and run:
where tests live and how they are discovered, which fixtures already exist,
how to install the project's test dependencies into a sandbox image, how to
run a single test file inside that container, and how to read the results
back. Generated tests must use the repository's existing framework so they
keep running in its normal CI without OpenHack.

pytest is the initial adapter; ``ADAPTERS`` is the extension point.
"""

from __future__ import annotations

import ast
import configparser
import fnmatch
import json
import os
import re
import tomllib
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

from .evidence import find_secrets, scrub_text
from .models import SecurityInvariant

SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env", ".tox",
    ".mypy_cache", ".pytest_cache", "dist", "build", ".eggs", "site-packages",
}
RESULTS_DIR = "/openhack-results"
MAX_RESULTS_BYTES = 5_000_000


class UnsupportedFrameworkError(Exception):
    """The repository has no test framework OpenHack can generate tests for."""


@dataclass
class FrameworkProfile:
    """What an adapter learned about the repository's existing test suite."""

    adapter: str
    language: str
    project_root: str
    test_dir: str
    file_patterns: list[str]
    config_file: Optional[str] = None
    dependency_files: list[str] = field(default_factory=list)
    installable_project: bool = False
    install_extras: list[str] = field(default_factory=list)
    fixtures: dict[str, str] = field(default_factory=dict)
    example_tests: list[str] = field(default_factory=list)
    app_frameworks: list[str] = field(default_factory=list)
    runtime_version: str = ""
    framework_in_dependencies: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CaseOutcome:
    name: str
    classname: str
    outcome: str  # passed | failed | error | skipped
    message: str = ""
    # For failures: whether the test failed an assertion (the invariant check)
    # rather than crashing on an unexpected exception.
    assertion: bool = False

    @property
    def base_name(self) -> str:
        return self.name.split("[", 1)[0]


@dataclass
class RunReport:
    cases: list[CaseOutcome] = field(default_factory=list)
    exit_code: Optional[int] = None
    timed_out: bool = False
    collection_error: Optional[str] = None
    output_tail: str = ""

    def outcomes(self) -> dict[str, str]:
        return {f"{c.classname}::{c.name}": c.outcome for c in self.cases}


class FrameworkAdapter(ABC):
    """Extension point for a test framework."""

    name: str = ""
    language: str = ""

    @abstractmethod
    def detect(self, repo_root: Path, hint_path: Optional[str] = None) -> Optional[FrameworkProfile]:
        """Return a profile if the repository uses this framework."""

    @abstractmethod
    def suggest_test_path(
        self, profile: FrameworkProfile, invariant: SecurityInvariant,
        exists: Callable[[str], bool],
    ) -> str:
        """A new, discoverable test path relative to the project root."""

    @abstractmethod
    def validate_test_path(self, profile: FrameworkProfile, relpath: str) -> Optional[str]:
        """Return why ``relpath`` is unacceptable, or None."""

    @abstractmethod
    def static_check(self, code: str) -> list[str]:
        """Policy violations in generated test code (never executes it)."""

    @abstractmethod
    def default_base_image(self, profile: FrameworkProfile) -> str:
        """Container base image when none is configured."""

    @abstractmethod
    def dockerfile(self, profile: FrameworkProfile, base_image: str) -> str:
        """Dockerfile that installs the project's test dependencies."""

    @abstractmethod
    def container_command(self, profile: FrameworkProfile, test_relpath: str) -> list[str]:
        """Command that runs one test file and writes results to RESULTS_DIR."""

    @abstractmethod
    def display_command(self, profile: FrameworkProfile, test_relpath: str) -> str:
        """How a developer runs the test with the repository's normal runner."""

    @abstractmethod
    def parse_results(self, results_dir: Path, exit_code: Optional[int], timed_out: bool) -> RunReport:
        """Read the results a container run copied back to the host."""

    def prompt_context(self, profile: FrameworkProfile) -> str:
        fixtures = ", ".join(
            f"`{name}` ({path})" for name, path in sorted(profile.fixtures.items())
        ) or "none found"
        examples = ", ".join(f"`{p}`" for p in profile.example_tests) or "none found"
        return (
            f"- Test framework: **{self.name}** ({self.language})\n"
            f"- Project root (relative to repository): `{profile.project_root}`\n"
            f"- Test directory: `{profile.test_dir}` · discovery patterns: "
            f"{', '.join(profile.file_patterns)}\n"
            f"- Configuration: `{profile.config_file or 'defaults'}`\n"
            f"- Existing fixtures: {fixtures}\n"
            f"- Existing tests to imitate: {examples}\n"
            f"- Application frameworks: {', '.join(profile.app_frameworks) or 'unknown'}\n"
        )


# ── Shared helpers ──────────────────────────────────────────────────

def _iter_files(root: Path, pattern: str, max_depth: int = 6):
    """Yield files matching ``pattern`` without descending into SKIP_DIRS.

    Pruning during the walk (rather than filtering rglob output) keeps large
    node_modules / virtualenv trees from being traversed at all.
    """
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root):
        depth = len(Path(dirpath).relative_to(root).parts)
        dirnames[:] = [
            d for d in dirnames
            if d not in SKIP_DIRS and not d.endswith(".egg-info")
        ] if depth < max_depth else []
        for name in filenames:
            if fnmatch.fnmatch(name, pattern):
                yield Path(dirpath) / name


def _read(path: Path, limit: int = 400_000) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fp:
            return fp.read(limit)
    except OSError:
        return ""


def _posix(path: Path | PurePosixPath | str) -> str:
    return PurePosixPath(*Path(path).parts).as_posix() if str(path) not in ("", ".") else "."


def _xml_is_safe(text: str) -> bool:
    lowered = text[:4096].lower()
    return "<!doctype" not in lowered and "<!entity" not in lowered


# ── pytest ──────────────────────────────────────────────────────────

_REQUIREMENTS_NAMES = (
    "requirements.txt", "requirements-dev.txt", "requirements_dev.txt",
    "requirements-test.txt", "requirements_test.txt", "requirements-tests.txt",
    "dev-requirements.txt", "test-requirements.txt", "requirements-testing.txt",
)
_TEST_EXTRAS = ("test", "tests", "testing", "dev")
_APP_FRAMEWORKS = (
    "flask", "django", "fastapi", "starlette", "quart", "aiohttp", "tornado",
    "bottle", "pyramid", "falcon", "sanic", "litestar",
)

# Imports and calls a regression test never needs: they reach outside the
# sandboxed process, defeat result integrity, or make outcomes timing-dependent.
_FORBIDDEN_MODULES = {
    "subprocess": "spawns processes",
    "socket": "opens raw network sockets",
    "requests": "makes real network requests (use the app's in-process test client)",
    "httpx": "makes real network requests (use the app's in-process test client)",
    "urllib": "makes real network requests (use the app's in-process test client)",
    "urllib3": "makes real network requests (use the app's in-process test client)",
    "aiohttp": "makes real network requests (use the app's in-process test client)",
    "http.client": "makes real network requests (use the app's in-process test client)",
    "ftplib": "makes network connections",
    "smtplib": "makes network connections",
    "telnetlib": "makes network connections",
    "paramiko": "makes network connections",
    "pty": "spawns terminals",
    "multiprocessing": "spawns processes",
    "ctypes": "loads native code",
    "atexit": "can tamper with test results at interpreter exit",
}
_FORBIDDEN_CALLS = {
    "eval": "evaluates dynamic code",
    "exec": "executes dynamic code",
    "compile": "compiles dynamic code",
    "__import__": "imports modules dynamically",
    "breakpoint": "blocks on a debugger",
    "os.system": "runs shell commands",
    "os.popen": "runs shell commands",
    "os.fork": "spawns processes",
    "os.kill": "signals processes",
    "os._exit": "terminates the test process",
    "sys.exit": "terminates the test process",
    "time.sleep": "sleeps (timing-dependent tests are flaky)",
    "asyncio.sleep": "sleeps (timing-dependent tests are flaky)",
    "pytest.skip": "skips the test, so it can never fail",
    "pytest.xfail": "marks the test as expected to fail",
    "pytest.importorskip": "can silently skip the test",
    "pytest.exit": "terminates the test session",
    "shutil.rmtree": "deletes files",
}
_FORBIDDEN_PREFIXES = ("os.exec", "os.spawn", "os.posix_spawn")
# Anything that lets a test report success (or "expected") without its
# assertions running. `mark.*` covers `from pytest import mark`.
_FORBIDDEN_MARKERS = {
    "pytest.mark.skip", "pytest.mark.skipif", "pytest.mark.xfail", "pytest.mark.flaky",
    "mark.skip", "mark.skipif", "mark.xfail", "mark.flaky",
    "unittest.skip", "unittest.skipIf", "unittest.skipUnless", "unittest.expectedFailure",
}


def _dotted(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def _module_root_forbidden(module: str) -> Optional[str]:
    for name, why in _FORBIDDEN_MODULES.items():
        if module == name or module.startswith(name + "."):
            return why
    return None


class PytestAdapter(FrameworkAdapter):
    name = "pytest"
    language = "python"

    # ── detection ──

    def detect(self, repo_root: Path, hint_path: Optional[str] = None) -> Optional[FrameworkProfile]:
        repo_root = repo_root.resolve()
        candidates = self._candidate_roots(repo_root)
        if not candidates:
            return None
        root = self._pick_root(candidates, hint_path)
        project = repo_root / root if root != "." else repo_root
        config_file, ini = self._pytest_config(project)
        test_files = [p for p in self._test_files(project, ini)]
        conftests = list(_iter_files(project, "conftest.py"))
        deps = self._dependency_files(project)
        pyproject = self._load_pyproject(project)
        dep_text = "\n".join(_read(project / d) for d in deps).lower()
        dep_text += "\n" + json.dumps(pyproject).lower()
        # Any pytest-ecosystem package (pytest, pytest-django, ...) counts.
        has_pytest_dep = bool(re.search(r"(?<![\w-])pytest", dep_text))
        if not (config_file or conftests or (test_files and has_pytest_dep)):
            return None

        test_dir = self._test_dir(project, ini, test_files)
        installable = bool(
            pyproject.get("project") or pyproject.get("tool", {}).get("poetry")
            or (project / "setup.py").is_file()
        )
        extras = [
            e for e in _TEST_EXTRAS
            if e in (pyproject.get("project", {}).get("optional-dependencies") or {})
        ]
        return FrameworkProfile(
            adapter=self.name,
            language=self.language,
            project_root=root,
            test_dir=test_dir,
            file_patterns=self._patterns(ini),
            config_file=config_file,
            dependency_files=deps,
            installable_project=installable,
            install_extras=extras,
            fixtures=self._fixtures(project, conftests),
            example_tests=self._examples(project, test_files, hint_path),
            app_frameworks=[
                fw for fw in _APP_FRAMEWORKS
                if re.search(rf"(?<![\w-]){fw}(?![\w-])", dep_text)
            ],
            runtime_version=self._python_version(project, pyproject),
            framework_in_dependencies=has_pytest_dep,
        )

    def _candidate_roots(self, repo_root: Path) -> list[str]:
        markers = ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", "setup.py",
                   "conftest.py", "requirements.txt")
        roots: set[str] = set()
        for marker in markers:
            for path in _iter_files(repo_root, marker, max_depth=3):
                rel = path.parent.relative_to(repo_root)
                # A conftest.py inside tests/ belongs to the project above it.
                if marker == "conftest.py" and rel.parts and rel.parts[-1] in ("tests", "test"):
                    rel = rel.parent
                roots.add(_posix(rel))
        return sorted(roots, key=lambda r: (r.count("/"), r))

    @staticmethod
    def _pick_root(candidates: list[str], hint_path: Optional[str]) -> str:
        if hint_path:
            hint = _posix(hint_path)
            best = None
            for root in candidates:
                if root == "." or hint == root or hint.startswith(root + "/"):
                    if best is None or len(root) > len(best):
                        best = root
            if best is not None:
                return best
        return "." if "." in candidates else candidates[0]

    @staticmethod
    def _pytest_config(project: Path) -> tuple[Optional[str], dict]:
        ini_path = project / "pytest.ini"
        if ini_path.is_file():
            parser = configparser.ConfigParser(interpolation=None)
            try:
                parser.read_string(_read(ini_path))
                return "pytest.ini", dict(parser["pytest"]) if parser.has_section("pytest") else {}
            except configparser.Error:
                return "pytest.ini", {}
        pyproject = project / "pyproject.toml"
        if pyproject.is_file():
            try:
                data = tomllib.loads(_read(pyproject))
            except tomllib.TOMLDecodeError:
                data = {}
            options = data.get("tool", {}).get("pytest", {}).get("ini_options")
            if isinstance(options, dict):
                return "pyproject.toml", {k: v for k, v in options.items()}
        for name, section in (("setup.cfg", "tool:pytest"), ("tox.ini", "pytest")):
            path = project / name
            if path.is_file():
                parser = configparser.ConfigParser(interpolation=None)
                try:
                    parser.read_string(_read(path))
                except configparser.Error:
                    continue
                if parser.has_section(section):
                    return name, dict(parser[section])
        return None, {}

    @staticmethod
    def _ini_list(value) -> list[str]:
        if isinstance(value, list):
            return [str(v) for v in value]
        if isinstance(value, str):
            return value.split()
        return []

    def _patterns(self, ini: dict) -> list[str]:
        return self._ini_list(ini.get("python_files")) or ["test_*.py", "*_test.py"]

    def _test_files(self, project: Path, ini: dict) -> list[Path]:
        found: list[Path] = []
        for pattern in self._patterns(ini):
            for path in _iter_files(project, pattern):
                if path.name != "conftest.py" and path not in found:
                    found.append(path)
        return found

    def _test_dir(self, project: Path, ini: dict, test_files: list[Path]) -> str:
        for entry in self._ini_list(ini.get("testpaths")):
            if (project / entry).is_dir():
                return _posix(PurePosixPath(entry))
        counts: dict[str, int] = {}
        for path in test_files:
            rel = _posix(path.parent.relative_to(project))
            counts[rel] = counts.get(rel, 0) + 1
        for preferred in ("tests", "test"):
            if preferred in counts or (project / preferred).is_dir():
                return preferred
        if counts:
            return max(counts.items(), key=lambda kv: (kv[1], -kv[0].count("/")))[0]
        return "tests"

    @staticmethod
    def _dependency_files(project: Path) -> list[str]:
        files: list[str] = [n for n in _REQUIREMENTS_NAMES if (project / n).is_file()]
        req_dir = project / "requirements"
        if req_dir.is_dir():
            files += sorted(_posix(p.relative_to(project)) for p in req_dir.glob("*.txt"))
        # Follow `-r other.txt` / `-c constraints.txt` includes so the image
        # build sees every file the listed requirements reference.
        queue = list(files)
        while queue:
            current = queue.pop()
            base = PurePosixPath(current).parent
            for line in _read(project / current).splitlines():
                match = re.match(r"\s*(?:-r|--requirement|-c|--constraint)\s*=?\s*(\S+)", line)
                if not match:
                    continue
                include = _posix(base / match.group(1))
                if include not in files and ".." not in include and (project / include).is_file():
                    files.append(include)
                    queue.append(include)
        return files

    @staticmethod
    def _load_pyproject(project: Path) -> dict:
        path = project / "pyproject.toml"
        if not path.is_file():
            return {}
        try:
            return tomllib.loads(_read(path))
        except tomllib.TOMLDecodeError:
            return {}

    @staticmethod
    def _fixtures(project: Path, conftests: list[Path]) -> dict[str, str]:
        fixtures: dict[str, str] = {}
        for conftest in conftests:
            try:
                tree = ast.parse(_read(conftest))
            except SyntaxError:
                continue
            rel = _posix(conftest.relative_to(project))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for dec in node.decorator_list:
                    target = dec.func if isinstance(dec, ast.Call) else dec
                    if _dotted(target) in ("pytest.fixture", "fixture"):
                        fixtures.setdefault(node.name, rel)
        return fixtures

    @staticmethod
    def _examples(project: Path, test_files: list[Path], hint_path: Optional[str]) -> list[str]:
        hint_words = set(re.findall(r"[a-z]{3,}", (hint_path or "").lower()))

        def score(path: Path) -> tuple[int, str]:
            words = set(re.findall(r"[a-z]{3,}", path.stem.lower()))
            return (-len(words & hint_words), path.as_posix())

        ranked = sorted(test_files, key=score)
        return [_posix(p.relative_to(project)) for p in ranked[:3]]

    @staticmethod
    def _python_version(project: Path, pyproject: dict) -> str:
        pinned = _read(project / ".python-version").strip().splitlines()
        if pinned:
            match = re.match(r"(\d+\.\d+)", pinned[0].strip())
            if match:
                return match.group(1)
        runtime = re.search(r"python-(\d+\.\d+)", _read(project / "runtime.txt"))
        if runtime:
            return runtime.group(1)
        requires = str(pyproject.get("project", {}).get("requires-python") or "")
        upper = re.search(r"<\s*3\.(\d+)", requires)
        if upper and int(upper.group(1)) <= 12:
            return f"3.{int(upper.group(1)) - 1}"
        return "3.12"

    # ── paths ──

    def suggest_test_path(self, profile, invariant, exists) -> str:
        slug = re.sub(r"[^a-z0-9]+", "_", f"{invariant.vulnerability_class}_{invariant.resource}".lower()).strip("_")
        base = PurePosixPath(profile.test_dir) / f"test_security_{slug or 'regression'}.py"
        candidate = base.as_posix()
        n = 2
        while exists(candidate):
            candidate = (base.parent / f"{base.stem}_{n}.py").as_posix()
            n += 1
        return candidate

    def validate_test_path(self, profile, relpath) -> Optional[str]:
        raw = (relpath or "").strip()
        if not raw:
            return "no test file path was provided"
        if "\\" in raw:
            return "use forward slashes in the test path"
        if not re.fullmatch(r"[A-Za-z0-9_./-]+", raw):
            return f"test path may only contain letters, digits, '_', '-', '.' and '/': {raw}"
        path = PurePosixPath(raw)
        if path.is_absolute() or ".." in path.parts or raw.startswith("~"):
            return f"test path must be relative to the project root without '..': {raw}"
        if path.suffix != ".py":
            return f"test file must be a .py file: {raw}"
        if path.name == "conftest.py":
            return "the regression test must be a new test module, not conftest.py"
        if not any(fnmatch.fnmatch(path.name, pat) for pat in profile.file_patterns):
            return (
                f"`{path.name}` would not be discovered by pytest "
                f"(patterns: {', '.join(profile.file_patterns)})"
            )
        test_dir = PurePosixPath(profile.test_dir)
        if profile.test_dir not in (".", "") and test_dir not in path.parents:
            return f"place the test under the existing test directory `{profile.test_dir}/`"
        return None

    # ── static policy ──

    def static_check(self, code: str) -> list[str]:
        violations: list[str] = []
        try:
            tree = ast.parse(code or "")
        except SyntaxError as exc:
            return [f"syntax error on line {exc.lineno}: {exc.msg}"]

        tests = [
            n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test")
        ]
        if not tests:
            violations.append("defines no test functions (names must start with `test`)")
        if not any(isinstance(n, ast.Assert) for n in ast.walk(tree)):
            violations.append("contains no assert statements")

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    why = _module_root_forbidden(alias.name)
                    if why:
                        violations.append(f"imports `{alias.name}`, which {why}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                why = _module_root_forbidden(module)
                if why:
                    violations.append(f"imports from `{module}`, which {why}")
                for alias in node.names:
                    dotted = f"{module}.{alias.name}"
                    if dotted in _FORBIDDEN_CALLS:
                        violations.append(f"imports `{dotted}`, which {_FORBIDDEN_CALLS[dotted]}")
                    elif dotted.startswith(_FORBIDDEN_PREFIXES):
                        violations.append(f"imports `{dotted}`, which spawns processes")
                    elif dotted in _FORBIDDEN_MARKERS:
                        violations.append(f"imports `{dotted}`, so the test could pass without running")
            elif isinstance(node, ast.Call):
                name = _dotted(node.func)
                if name in _FORBIDDEN_CALLS:
                    violations.append(f"calls `{name}()`, which {_FORBIDDEN_CALLS[name]}")
                elif name.startswith(_FORBIDDEN_PREFIXES):
                    violations.append(f"calls `{name}()`, which spawns processes")
                elif name.endswith(".skipTest"):
                    violations.append(f"calls `{name}()`, which skips the test, so it can never fail")
            elif isinstance(node, ast.Attribute):
                name = _dotted(node)
                if name in _FORBIDDEN_MARKERS:
                    violations.append(f"uses `{name}`, so the test could pass without running")
            elif isinstance(node, ast.ExceptHandler):
                caught = _dotted(node.type) if node.type is not None else ""
                names = {caught}
                if isinstance(node.type, ast.Tuple):
                    names = {_dotted(elt) for elt in node.type.elts}
                if node.type is None or names & {"AssertionError", "BaseException", "Exception"}:
                    violations.append(
                        "catches AssertionError/Exception (or uses a bare except), "
                        "which can swallow the invariant check"
                    )
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("pytest_"):
                    violations.append(f"defines pytest hook `{node.name}`")

        for secret in find_secrets(code or ""):
            violations.append(
                f"embeds a {secret}; tests must create their own throwaway credentials "
                "and never contain captured exploit data"
            )
        # De-duplicate while keeping order so feedback stays readable.
        return list(dict.fromkeys(violations))

    # ── sandbox image + execution ──

    def default_base_image(self, profile) -> str:
        return f"python:{profile.runtime_version or '3.12'}-slim"

    def dockerfile(self, profile, base_image) -> str:
        root = profile.project_root
        workdir = "/workspace" if root == "." else f"/workspace/{root}"
        lines = [
            "# Generated by OpenHack to validate a security regression test.",
            f"FROM {base_image}",
            "ENV PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1 "
            "PIP_NO_INPUT=1 PIP_ROOT_USER_ACTION=ignore",
            f"WORKDIR {workdir}",
        ]
        if profile.dependency_files:
            for dep in profile.dependency_files:
                src = dep if root == "." else f"{root}/{dep}"
                lines.append(f"COPY {json.dumps([src, f'{workdir}/{dep}'])}")
            flags = " ".join(f"-r {json.dumps(d)}" for d in profile.dependency_files)
            lines.append(f"RUN pip install --no-cache-dir {flags}")
        lines.append('COPY [".", "/workspace"]')
        if profile.installable_project:
            target = "." + (f"[{','.join(profile.install_extras)}]" if profile.install_extras else "")
            lines.append(
                f"RUN pip install --no-cache-dir -e {json.dumps(target)} "
                "|| echo 'openhack: project install failed; continuing with listed requirements'"
            )
        lines += [
            'RUN python -c "import pytest" 2>/dev/null || pip install --no-cache-dir pytest',
            f"RUN mkdir -p {RESULTS_DIR} && chown -R 65534:65534 /workspace {RESULTS_DIR}",
        ]
        return "\n".join(lines) + "\n"

    def container_command(self, profile, test_relpath) -> list[str]:
        # Output goes to a file so a noisy test can't flood the host, and the
        # repository's own pytest configuration (rootdir, conftest, plugins)
        # still applies. --maxfail overrides any `-x` in addopts so every
        # declared test reports an outcome.
        script = (
            'python -m pytest "$1" -p no:cacheprovider --maxfail=100000 '
            f"-o junit_family=xunit2 --junitxml={RESULTS_DIR}/junit.xml "
            f"> {RESULTS_DIR}/output.txt 2>&1"
        )
        return ["sh", "-c", script, "openhack-pytest", test_relpath]

    def display_command(self, profile, test_relpath) -> str:
        cmd = f"python -m pytest {test_relpath}"
        return cmd if profile.project_root == "." else f"cd {profile.project_root} && {cmd}"

    def parse_results(self, results_dir: Path, exit_code: Optional[int], timed_out: bool) -> RunReport:
        output = ""
        out_path = results_dir / "output.txt"
        if out_path.is_file():
            try:
                with open(out_path, "rb") as fp:
                    fp.seek(0, 2)
                    size = fp.tell()
                    fp.seek(max(0, size - 6000))
                    output = scrub_text(fp.read().decode("utf-8", errors="replace"))
            except OSError:
                output = ""
        report = RunReport(exit_code=exit_code, timed_out=timed_out, output_tail=output)
        if timed_out:
            return report
        junit = results_dir / "junit.xml"
        if not junit.is_file():
            report.collection_error = (
                f"pytest produced no results (exit code {exit_code}). Output:\n{output[-2000:]}"
            )
            return report
        if junit.stat().st_size > MAX_RESULTS_BYTES:
            report.collection_error = "test results file is unreasonably large; rejected"
            return report
        text = junit.read_text(encoding="utf-8", errors="replace")
        if not _xml_is_safe(text):
            report.collection_error = "test results contained a DTD/entity declaration; rejected"
            return report
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            report.collection_error = f"could not parse test results: {exc}"
            return report

        for case in root.iter("testcase"):
            name = case.get("name", "")
            classname = case.get("classname", "")
            outcome, message, assertion = "passed", "", False
            for child in case:
                tag = child.tag
                detail = scrub_text(((child.get("message") or "") + "\n" + (child.text or "")).strip())
                if tag == "failure":
                    outcome = "failed"
                    assertion = "AssertionError" in detail or detail.startswith("assert")
                    message = detail[-1500:]
                elif tag == "error":
                    outcome = "error"
                    message = detail[-1500:]
                elif tag == "skipped":
                    outcome = "skipped"
                    message = detail[-300:]
            if outcome == "error" and (not classname or "collection failure" in message):
                report.collection_error = message or "collection failed"
                continue
            report.cases.append(CaseOutcome(name, classname, outcome, message, assertion))
        if not report.cases and report.collection_error is None:
            report.collection_error = f"no tests were collected. Output:\n{output[-2000:]}"
        return report


ADAPTERS: list[FrameworkAdapter] = [PytestAdapter()]


def _describe_unsupported(repo_root: Path) -> str:
    """Name the test framework we found but cannot drive yet, if any."""
    for pkg in _iter_files(repo_root, "package.json", max_depth=3):
        try:
            data = json.loads(_read(pkg))
        except (json.JSONDecodeError, ValueError):
            continue
        deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
        script = str((data.get("scripts") or {}).get("test") or "")
        for runner in ("jest", "vitest", "mocha", "ava", "tap", "jasmine", "playwright"):
            if runner in deps or runner in script:
                return f"JavaScript test framework `{runner}`"
    if (repo_root / "go.mod").is_file():
        return "Go `testing`"
    if (repo_root / "pom.xml").is_file() or (repo_root / "build.gradle").is_file():
        return "JUnit (JVM)"
    if "rspec" in _read(repo_root / "Gemfile").lower():
        return "RSpec"
    if (repo_root / "composer.json").is_file():
        return "PHPUnit"
    if (repo_root / "manage.py").is_file():
        return "Django's built-in test runner (pytest-django projects are supported)"
    return ""


def detect_framework(repo_root: Path, hint_path: Optional[str] = None) -> tuple[FrameworkAdapter, FrameworkProfile]:
    """Find the adapter for the repository's existing test suite.

    Raises UnsupportedFrameworkError with an explanation when there is no
    existing suite we can drive — OpenHack does not bootstrap a framework the
    repository does not already use.
    """
    for adapter in ADAPTERS:
        profile = adapter.detect(repo_root, hint_path)
        if profile is not None:
            return adapter, profile
    supported = ", ".join(a.name for a in ADAPTERS)
    other = _describe_unsupported(repo_root)
    if other:
        raise UnsupportedFrameworkError(
            f"the repository's tests use {other}, which is not supported yet "
            f"(supported: {supported})"
        )
    raise UnsupportedFrameworkError(
        "no existing test suite was detected (looked for pytest configuration, "
        f"conftest.py and test files); supported frameworks: {supported}"
    )
