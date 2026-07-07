"""Tests for the @-path reference completer (OpenCode-style file/dir picker)."""

import os

import pytest

from openhack.tui import OpenHackCompleter


# ------------------------------------------------------- token detection

@pytest.mark.parametrize("text,expected", [
    ("@", ""),
    ("look at @", ""),
    ("review @openhack/tu", "openhack/tu"),
    ("@src/main.py", "src/main.py"),
    ("email me at foo@bar", None),   # mid-word @ (email) is not a reference
    ("a@b", None),
    ("check @foo and @bar", "bar"),  # the token at the cursor (end)
    ("no reference here", None),
    ("@path with space", None),      # whitespace ends the token
])
def test_active_at_token(text, expected):
    assert OpenHackCompleter._active_at_token(text) == expected


# ------------------------------------------------------- path matching

def _index(entries):
    c = OpenHackCompleter()
    c._at_index = entries
    return c


SAMPLE = [
    ("openhack/", True),
    ("openhack/agents/", True),
    ("openhack/agents/llm.py", False),
    ("openhack/tui.py", False),
    ("openhack/config.py", False),
    ("tests/", True),
    ("tests/test_tui.py", False),
    ("README.md", False),
]


def test_path_completion_basename_substring():
    c = _index(SAMPLE)
    out = [x.text for x in c._path_completions("tui")]
    assert "@openhack/tui.py" in out
    # basename match ranks the file with 'tui' in its name first
    assert out[0] == "@openhack/tui.py"


def test_path_completion_nested_prefix():
    c = _index(SAMPLE)
    out = [x.text for x in c._path_completions("openhack/agents/")]
    assert "@openhack/agents/llm.py" in out


def test_path_completion_inserts_at_and_replaces_token():
    c = _index(SAMPLE)
    comps = list(c._path_completions("tui"))
    hit = next(x for x in comps if x.text == "@openhack/tui.py")
    # Replaces '@tui' (partial length + the leading @).
    assert hit.start_position == -4
    assert hit.display_meta_text == "file"


def test_path_completion_dir_meta_and_trailing_slash():
    c = _index(SAMPLE)
    comps = list(c._path_completions("agents"))
    d = next(x for x in comps if x.text == "@openhack/agents/")
    assert d.display_meta_text == "dir"


def test_path_completion_empty_query_lists_toplevel_first():
    c = _index(SAMPLE)
    out = [x.text for x in c._path_completions("")]
    # Top-level entries (fewest slashes) come before deeper ones.
    assert out[0] in ("@openhack/", "@tests/", "@README.md")


# ------------------------------------------------------- navigation (../ etc)

def test_at_dotdot_offers_parent():
    c = OpenHackCompleter()
    out = list(c._path_completions(".."))
    assert out[0].text == "@../"
    assert out[0].display_meta_text == "dir"


def test_at_parent_lists_siblings(tmp_path, monkeypatch):
    # tmp_path/
    #   proj/   <- cwd
    #   sibling-a/  sibling-b/  note.txt
    (tmp_path / "proj").mkdir()
    (tmp_path / "sibling-a").mkdir()
    (tmp_path / "sibling-b").mkdir()
    (tmp_path / "note.txt").write_text("x")
    monkeypatch.chdir(tmp_path / "proj")
    c = OpenHackCompleter()

    out = [x.text for x in c._path_completions("../")]
    assert "@../sibling-a/" in out
    assert "@../sibling-b/" in out
    assert "@../note.txt" in out
    # Directories are listed before files.
    assert out.index("@../sibling-a/") < out.index("@../note.txt")


def test_at_parent_prefix_filters(tmp_path, monkeypatch):
    (tmp_path / "proj").mkdir()
    (tmp_path / "openhack-cli").mkdir()
    (tmp_path / "openhack-inference").mkdir()
    (tmp_path / "unrelated").mkdir()
    monkeypatch.chdir(tmp_path / "proj")
    c = OpenHackCompleter()

    out = [x.text for x in c._path_completions("../openhack-")]
    assert set(out) == {"@../openhack-cli/", "@../openhack-inference/"}


def test_at_navigational_meta_and_replacement(tmp_path, monkeypatch):
    (tmp_path / "proj").mkdir()
    (tmp_path / "sib").mkdir()
    monkeypatch.chdir(tmp_path / "proj")
    c = OpenHackCompleter()
    hit = next(x for x in c._path_completions("../s") if x.text == "@../sib/")
    assert hit.display_meta_text == "dir"
    # '@../s' is 5 chars — the whole token is replaced.
    assert hit.start_position == -5


def test_build_index_skips_noise_and_dotfiles(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.js").write_text("x")
    (tmp_path / ".secret").write_text("x")
    monkeypatch.chdir(tmp_path)
    c = OpenHackCompleter()
    idx = c._build_at_index()
    paths = {p for p, _ in idx}
    assert "src/app.py" in paths
    assert not any("node_modules" in p for p in paths)
    assert not any(".secret" in p for p in paths)
