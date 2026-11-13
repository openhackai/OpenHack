"""Tests for the deterministic static pre-validator."""

from openhack.static_validator import run_static_validation, validate_finding_basic


def _write(fs_tools, rel_path, content):
    target = fs_tools.jail_dir / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)


def test_finding_inside_js_block_comment_is_rejected(fs_tools):
    """A sink reported inside a /* ... */ block comment is not real code.

    The validator's stated purpose is to reject findings where the pattern
    sits in a comment rather than actual code. Line comments (//) are
    handled, but block comments were not, so a finding pointing inside one
    passed every layer.
    """
    _write(
        fs_tools,
        "app/handler.js",
        "function handler(req, res) {\n"
        "  /*\n"
        "   * Legacy debug path, disabled:\n"
        "   * eval(req.query.code)\n"
        "   */\n"
        "  const q = sanitize(req.query.code);\n"
        "  res.send(q);\n"
        "}\n",
    )
    finding = {
        "file_path": "app/handler.js",
        "line_number": 4,  # the eval(...) line, inside the block comment
        "category": "rce",
    }
    is_valid, reason = validate_finding_basic(finding, fs_tools)
    assert not is_valid
    assert "comment" in reason.lower()


def test_finding_on_single_line_block_comment_is_rejected(fs_tools):
    _write(
        fs_tools,
        "app/util.js",
        "/* eval(userInput) -- kept for reference, do not enable */\n"
        "export function util() { return 1; }\n",
    )
    finding = {
        "file_path": "app/util.js",
        "line_number": 1,
        "category": "rce",
    }
    is_valid, reason = validate_finding_basic(finding, fs_tools)
    assert not is_valid
    assert "comment" in reason.lower()


def test_real_sink_after_a_block_comment_still_passes(fs_tools):
    """The block-comment state must reset at */ — real code after a comment
    block is not a comment."""
    _write(
        fs_tools,
        "app/handler.js",
        "function handler(req, res) {\n"
        "  /* disabled: eval(req.query.old) */\n"
        "  eval(req.query.code);\n"
        "}\n",
    )
    finding = {
        "file_path": "app/handler.js",
        "line_number": 3,
        "category": "rce",
    }
    is_valid, reason = validate_finding_basic(finding, fs_tools)
    assert is_valid, reason


def test_line_comment_still_rejected(fs_tools):
    _write(
        fs_tools,
        "app/handler.js",
        "function handler(req) {\n"
        "  // eval(req.query.code)\n"
        "  return 1;\n"
        "}\n",
    )
    finding = {
        "file_path": "app/handler.js",
        "line_number": 2,
        "category": "rce",
    }
    is_valid, reason = validate_finding_basic(finding, fs_tools)
    assert not is_valid
    assert "comment" in reason.lower()


def test_run_static_validation_drops_block_comment_finding(fs_tools):
    """End-to-end: a block-comment finding must not survive the pipeline."""
    _write(
        fs_tools,
        "app/handler.js",
        "function handler(req, res) {\n"
        "  /*\n"
        "   * eval(req.query.code)\n"
        "   */\n"
        "  res.send('ok');\n"
        "}\n",
    )
    findings = [
        {
            "file_path": "app/handler.js",
            "line_number": 3,
            "category": "rce",
        }
    ]
    passed, rejected, stats = run_static_validation(findings, fs_tools)
    assert passed == []
    assert len(rejected) == 1
    assert stats["basic_rejected"] == 1
