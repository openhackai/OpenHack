"""Revision workspaces: materializing vulnerable and remediated code safely."""

import os
import shutil
import subprocess

import pytest

from openhack.regression import workspace
from openhack.regression.workspace import (
    RevisionSpec,
    WorkspaceError,
    git_info,
    is_excluded_path,
    materialize,
    new_file_patch,
)
from tests.conftest import write_file
from tests.regression_support import FIX_BRANCH, FIXTURE_APP, git, make_fixture_repo


@pytest.fixture
def fixture_repo(tmp_path):
    return make_fixture_repo(tmp_path)


def test_git_revisions_materialize_exact_commit_contents(fixture_repo, tmp_path):
    repo = fixture_repo["repo"]
    vulnerable = materialize(repo, RevisionSpec.git(fixture_repo["vulnerable_sha"]), tmp_path / "v")
    fixed = materialize(repo, RevisionSpec.git(FIX_BRANCH), tmp_path / "f")

    assert vulnerable.resolved == fixture_repo["vulnerable_sha"]
    assert fixed.resolved == fixture_repo["fixed_sha"]
    assert (vulnerable.root / "app.py").read_text() == (FIXTURE_APP / "vulnerable" / "app.py").read_text()
    assert (fixed.root / "app.py").read_text() == (FIXTURE_APP / "fixed_app.py").read_text()
    assert (fixed.root / "tests" / "conftest.py").is_file()
    assert vulnerable.fingerprint != fixed.fingerprint
    assert fixed.label == f"{FIX_BRANCH} ({fixture_repo['fixed_sha'][:12]})"
    assert vulnerable.label == fixture_repo["vulnerable_sha"][:12]
    # The user's checkout is untouched.
    assert git(repo, "status", "--porcelain") == ""
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"


def test_git_revisions_ignore_export_attributes(fixture_repo, tmp_path):
    repo = fixture_repo["repo"]
    write_file(repo, ".gitattributes", "tests export-ignore\napp.py export-subst\n")
    git(repo, "add", ".gitattributes")
    git(repo, "commit", "-q", "-m", "release tarball settings")
    copy = materialize(repo, RevisionSpec.git("HEAD"), tmp_path / "c")
    assert (copy.root / "tests" / "test_notes.py").is_file()
    assert (copy.root / "app.py").read_text() == (repo / "app.py").read_text()


def test_identical_trees_share_a_fingerprint(fixture_repo, tmp_path):
    repo = fixture_repo["repo"]
    a = materialize(repo, RevisionSpec.git("main"), tmp_path / "a")
    b = materialize(repo, RevisionSpec.worktree(), tmp_path / "b")
    assert a.fingerprint == b.fingerprint


def test_worktree_snapshot_honors_gitignore_and_skips_credentials(fixture_repo, tmp_path):
    repo = fixture_repo["repo"]
    write_file(repo, ".gitignore", "build/\n")
    write_file(repo, "build/artifact.py", "x = 1\n")
    write_file(repo, "untracked_module.py", "y = 2\n")
    write_file(repo, ".env", "SECRET_KEY=real-production-secret\n")
    write_file(repo, ".env.example", "SECRET_KEY=change-me\n")
    write_file(repo, "certs/server.key", "-----BEGIN PRIVATE KEY-----\n")
    write_file(repo, "node_modules/left-pad/index.js", "module.exports = 1\n")
    os.symlink("/etc/passwd", repo / "passwd_link")

    copy = materialize(repo, RevisionSpec.worktree(), tmp_path / "w")
    files = {p.relative_to(copy.root).as_posix() for p in copy.root.rglob("*") if p.is_file()}

    assert "untracked_module.py" in files and ".env.example" in files and "app.py" in files
    for absent in (".env", "certs/server.key", "build/artifact.py", "passwd_link"):
        assert absent not in files
    assert not any(f.startswith(("node_modules/", ".git/")) for f in files)
    assert copy.resolved.startswith("working tree @ ")


def test_tracked_directory_replaced_by_a_symlink_is_not_followed(fixture_repo, tmp_path):
    repo = fixture_repo["repo"]
    outside = tmp_path / "outside"
    write_file(outside, "conftest.py", "HOST_SECRET = 'outside the checkout'\n")
    write_file(outside, "test_notes.py", "HOST_SECRET = 'outside the checkout'\n")
    shutil.rmtree(repo / "tests")
    os.symlink(outside, repo / "tests")
    # The index still lists the directory's files under the symlink.
    assert "tests/conftest.py" in git(repo, "ls-files", "--cached").splitlines()

    copy = materialize(repo, RevisionSpec.worktree(), tmp_path / "w")
    assert not (copy.root / "tests").exists()
    assert (copy.root / "app.py").is_file()


def test_directory_revisions(tmp_path):
    write_file(tmp_path / "fixed", "app.py", "print('fixed')\n")
    write_file(tmp_path / "fixed", ".venv/lib/x.py", "")
    spec = RevisionSpec.parse("fixed", base_dir=tmp_path)
    assert spec.kind == "directory"
    copy = materialize(tmp_path, spec, tmp_path / "out")
    assert copy.file_count == 1 and (copy.root / "app.py").is_file()


def test_revision_spec_parse_defaults_to_git():
    assert RevisionSpec.parse("fix/idor-notes") == RevisionSpec.git("fix/idor-notes")
    with pytest.raises(WorkspaceError):
        RevisionSpec.parse("  ")


def test_unknown_git_revision_is_a_workspace_error(fixture_repo, tmp_path):
    with pytest.raises(WorkspaceError, match="is not a commit"):
        materialize(fixture_repo["repo"], RevisionSpec.git("no-such-branch"), tmp_path / "x")


def test_git_revision_outside_a_repository(tmp_path):
    write_file(tmp_path / "plain", "app.py", "")
    with pytest.raises(WorkspaceError, match="not a git repository"):
        materialize(tmp_path / "plain", RevisionSpec.git("main"), tmp_path / "x")


def test_size_limits_are_enforced(fixture_repo, tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "MAX_FILES", 2)
    with pytest.raises(WorkspaceError, match="more than 2 files"):
        materialize(fixture_repo["repo"], RevisionSpec.git("main"), tmp_path / "x")


def test_subdirectory_targets_use_the_matching_subtree(tmp_path):
    repo = tmp_path / "mono"
    write_file(repo, "services/api/app.py", "print('api')\n")
    write_file(repo, "web/index.js", "1\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "-c", "user.email=a@b.c", "-c", "user.name=a", "add", "-A")
    git(repo, "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-q", "-m", "init")
    target = repo / "services" / "api"
    assert git_info(target)["prefix"] == "services/api"
    copy = materialize(target, RevisionSpec.git("HEAD"), tmp_path / "x")
    assert {p.name for p in copy.root.iterdir()} == {"app.py"}


@pytest.mark.parametrize("path,excluded", [
    ("app.py", False), (".env.example", False), ("config/settings.py", False),
    (".env", True), (".env.production", True), ("keys/deploy.pem", True),
    ("home/.ssh/id_ed25519", True), (".aws/credentials", True), (".npmrc", True),
    ("pkg.egg-info/PKG-INFO", True), (".openhack-sandbox-override-x.yml", True),
])
def test_is_excluded_path(path, excluded):
    assert is_excluded_path(path) is excluded


def test_new_file_patch_applies_with_git(fixture_repo):
    repo = fixture_repo["repo"]
    content = "def test_x():\n    assert True"
    patch = repo.parent / "add-test.patch"
    patch.write_text(new_file_patch("tests/test_security_idor_note.py", content))
    subprocess.run(["git", "-C", str(repo), "apply", "--check", str(patch)], check=True)
    subprocess.run(["git", "-C", str(repo), "apply", str(patch)], check=True)
    assert (repo / "tests" / "test_security_idor_note.py").read_text() == content + "\n"
