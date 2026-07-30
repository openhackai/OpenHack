import pytest
from pathlib import Path
from tests.conftest import write_file

from openhack.tools.filesystem import FileSystemTools
from openhack.tools.registry import ToolRegistry


class TestReadFile:
    def test_reads_file_with_line_numbers(self, tmp_path):
        write_file(tmp_path, "hello.py", "line1\nline2\nline3\n")
        fs = FileSystemTools(jail_dir=tmp_path)
        result = fs.read_file("hello.py")
        assert result["total_lines"] == 3
        assert "line1" in result["content"]

    def test_offset_and_limit(self, tmp_path):
        content = "\n".join(f"line{i}" for i in range(100))
        write_file(tmp_path, "big.txt", content)
        fs = FileSystemTools(jail_dir=tmp_path)
        result = fs.read_file("big.txt", offset=10, limit=5)
        assert result["lines_returned"] == 5
        assert "line10" in result["content"]

    def test_file_not_found(self, tmp_path):
        fs = FileSystemTools(jail_dir=tmp_path)
        result = fs.read_file("nope.txt")
        assert "error" in result

    def test_binary_file_detected(self, tmp_path):
        write_file(tmp_path, "image.png", "fake png data")
        fs = FileSystemTools(jail_dir=tmp_path)
        result = fs.read_file("image.png")
        assert result.get("binary") is True


class TestJailEnforcement:
    def test_blocks_path_traversal(self, tmp_path):
        fs = FileSystemTools(jail_dir=tmp_path)
        with pytest.raises(PermissionError):
            fs._resolve_safe_path("../../etc/passwd")

    def test_blocks_absolute_path_escape(self, tmp_path):
        fs = FileSystemTools(jail_dir=tmp_path)
        with pytest.raises(PermissionError):
            fs._resolve_safe_path("/etc/passwd")

    def test_allows_nested_path(self, tmp_path):
        write_file(tmp_path, "src/app/main.py", "code")
        fs = FileSystemTools(jail_dir=tmp_path)
        result = fs.read_file("src/app/main.py")
        assert "error" not in result

    def test_blocks_sibling_with_same_string_prefix(self, tmp_path):
        jail = tmp_path / "root"
        sibling = tmp_path / "root-secret"
        jail.mkdir()
        sibling.mkdir()
        (sibling / "secret.txt").write_text("hidden")
        fs = FileSystemTools(jail_dir=jail)
        with pytest.raises(PermissionError):
            fs._resolve_safe_path(str(sibling / "secret.txt"))

    def test_interactive_registry_can_read_outside_target(self, tmp_path):
        target = tmp_path / "target"
        target.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("available")
        registry = ToolRegistry(target, include_agent_tools=True)
        result = registry.execute_tool("read_file", {"path": str(outside)})
        assert "available" in result["content"]
        assert result["path"] == str(outside)

    def test_scan_registry_remains_jailed(self, tmp_path):
        target = tmp_path / "target"
        target.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("hidden")
        registry = ToolRegistry(target)
        result = registry.execute_tool("read_file", {"path": str(outside)})
        assert "error" in result


class TestListDir:
    def test_lists_entries(self, tmp_path):
        write_file(tmp_path, "a.py", "x")
        write_file(tmp_path, "b.js", "y")
        (tmp_path / "subdir").mkdir()
        fs = FileSystemTools(jail_dir=tmp_path)
        result = fs.list_dir(".")
        names = {e["name"] for e in result["entries"]}
        assert "a.py" in names
        assert "subdir" in names


class TestGlob:
    def test_finds_matching_files(self, tmp_path):
        write_file(tmp_path, "src/a.py", "x")
        write_file(tmp_path, "src/b.py", "y")
        write_file(tmp_path, "src/c.js", "z")
        fs = FileSystemTools(jail_dir=tmp_path)
        result = fs.glob("**/*.py")
        assert len(result["matches"]) == 2


class TestGrep:
    def test_finds_pattern(self, tmp_path):
        write_file(tmp_path, "app.py", "from flask import Flask\napp = Flask(__name__)\n")
        fs = FileSystemTools(jail_dir=tmp_path)
        result = fs.grep("flask")
        assert len(result["matches"]) > 0

    def test_no_matches(self, tmp_path):
        write_file(tmp_path, "app.py", "hello world\n")
        fs = FileSystemTools(jail_dir=tmp_path)
        result = fs.grep("nonexistent_string_xyz")
        assert len(result["matches"]) == 0
