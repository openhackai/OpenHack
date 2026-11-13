"""
Deterministic pre-validator for hunter findings.

Runs BEFORE the LLM validator to filter out findings that are provably
wrong without any LLM reasoning. Two layers:

Layer A (basic checks):
  - Does the file exist?
  - Does the reported line actually contain the claimed pattern?
  - Is the pattern in a comment or string literal, not actual code?
  - Is it in a test/fixture file?

Layer B (tree-sitter sink verification):
  - Parse the function containing the reported sink
  - Check whether any parameter traces back to user input (req, request, params, body, etc.)
  - If the sink only receives constants/config values, reject the finding

Returns: filtered list of findings with rejection reasons attached to dropped ones.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)

# --- User input source identifiers ---
_USER_INPUT_NAMES = {
    # JS/TS
    "req", "request", "params", "query", "body", "searchParams",
    "ctx", "input", "args", "data", "payload", "formData",
    "req.body", "req.query", "req.params", "request.body",
    "request.form", "request.args", "request.json", "request.data",
    "request.GET", "request.POST", "request.FILES",
    # Python
    "self.request",
}

# --- Sink patterns per category ---
_CATEGORY_SINK_PATTERNS: dict[str, list[re.Pattern]] = {
    "sql_injection": [
        re.compile(r"(?:query|execute|raw|text|cursor\.execute|db\.execute|\.raw\(|\.extra\(|RawSQL)", re.I),
        re.compile(r"(?:SELECT|INSERT|UPDATE|DELETE|FROM|WHERE)", re.I),
    ],
    "xss": [
        re.compile(r"(?:dangerouslySetInnerHTML|innerHTML|document\.write|v-html|mark_safe|Markup\(|\|safe)", re.I),
    ],
    "rce": [
        re.compile(r"(?:eval|exec|Function\(|child_process|subprocess|os\.system|os\.popen|spawn|execFile)", re.I),
    ],
    "command_injection": [
        re.compile(r"(?:exec|spawn|subprocess|os\.system|os\.popen|child_process|shell=True)", re.I),
    ],
    "ssrf": [
        re.compile(r"(?:fetch|axios|http\.request|urllib|requests\.get|requests\.post|httpx)", re.I),
    ],
    "ssti": [
        re.compile(r"(?:render_template_string|Template\(|from_string|ejs\.render|nunjucks\.renderString|pug\.render)", re.I),
    ],
    "path_traversal": [
        re.compile(r"(?:sendFile|send_file|FileResponse|open\(|readFile|createReadStream|res\.download)", re.I),
    ],
    "open_redirect": [
        re.compile(r"(?:redirect|302|Location|NextResponse\.redirect|res\.redirect)", re.I),
    ],
    "prototype_pollution": [
        re.compile(r"(?:__proto__|Object\.assign|lodash\.merge|_.merge|defaultsDeep|deepmerge)", re.I),
    ],
}

# --- Comment patterns ---
_JS_LINE_COMMENT = re.compile(r"^\s*//")
_JS_BLOCK_COMMENT_START = re.compile(r"/\*")
_JS_BLOCK_COMMENT_END = re.compile(r"\*/")
_PY_LINE_COMMENT = re.compile(r"^\s*#")
_PY_DOCSTRING = re.compile(r'^\s*(?:"""|\'\'\')')

_TEST_DIR_PATTERNS = re.compile(
    r"(?:^|/)(?:test|tests|__tests__|spec|__mocks__|fixtures|mocks|__fixtures__|e2e|cypress|playwright)(?:/|$)",
    re.I,
)

_NON_PRODUCTION_DIR_PATTERNS = re.compile(
    r"(?:^|/)(?:"
    r"test|tests|__tests__|spec|__mocks__|fixtures|__fixtures__|e2e|cypress|playwright|"
    r"cli|CLI|docs|documentation|examples?|samples?|scripts|tools|devtools|"
    r"benchmarks?|integration-tests|\.storybook|stories"
    r")(?:/|$)",
    re.I,
)

# Signals that code is intentional design, not a vulnerability
_INTENT_COMMENT_PATTERNS = [
    re.compile(r"@since\s+\d+\.\d+", re.I),              # Versioned API — deliberate
    re.compile(r"intentionally?\s+(?:public|open|exposed|disabled|skipped)", re.I),
    re.compile(r"by\s+design", re.I),
    re.compile(r"public\s+(?:endpoint|api|route)", re.I),
    re.compile(r"no\s+auth(?:entication)?\s+(?:required|needed)", re.I),
    re.compile(r"allow\s+(?:unauthenticated|anonymous|public)", re.I),
    re.compile(r"fallback\s+(?:for|in)\s+(?:dev|development|test)", re.I),
    re.compile(r"default\s+(?:for|in)\s+(?:dev|development|test)", re.I),
    re.compile(r"only\s+(?:in|for|when)\s+(?:dev|development|test|non-prod)", re.I),
]

# Code patterns that indicate dev-only fallbacks, not production secrets
_DEV_FALLBACK_PATTERNS = [
    re.compile(r"(?:NODE_ENV|RAILS_ENV|FLASK_ENV|APP_ENV)\s*[!=]=\s*['\"](?:production|prod)['\"]", re.I),
    re.compile(r"process\.env\.\w+\s*\|\|\s*['\"]", re.I),  # env || "default"
    re.compile(r"process\.env\.\w+\s*\|\|\s*\w+", re.I),    # env || CONSTANT_NAME
    re.compile(r"os\.environ\.get\(\s*['\"][^'\"]+['\"]\s*,\s*['\"]", re.I),  # os.environ.get("X", "default")
    re.compile(r"ENV\.fetch\(\s*['\"][^'\"]+['\"]\s*,\s*['\"]", re.I),  # Ruby ENV.fetch("X", "default")
    re.compile(r"(?:const|let|var)\s+DEFAULT_\w*\s*=\s*['\"]", re.I),  # const DEFAULT_SECRET = "value"
    re.compile(r"\?\?\s*DEFAULT_", re.I),                     # ?? DEFAULT_CONSTANT (nullish coalescing)
]


def _is_test_file(file_path: str) -> bool:
    """Check if a file is a test/fixture file."""
    if _TEST_DIR_PATTERNS.search(file_path):
        return True
    basename = os.path.basename(file_path).lower()
    return any(basename.startswith(p) or basename.endswith(p) for p in [
        "test_", "_test.", ".test.", ".spec.", "conftest.", "fixture",
    ])


def _is_non_production_path(file_path: str) -> bool:
    """Check if a file is in a non-production directory (tests, CLI, docs, examples, etc.)."""
    if _NON_PRODUCTION_DIR_PATTERNS.search(file_path):
        return True
    basename = os.path.basename(file_path).lower()
    return any(basename.startswith(p) or basename.endswith(p) for p in [
        "test_", "_test.", ".test.", ".spec.", "conftest.", "fixture",
    ])


def _check_developer_intent(content: str, line_number: Optional[int], category: str) -> Optional[str]:
    """Check if surrounding code shows intentional design, returning a reason if so.

    Scans a window around the reported line for comments/code that indicate
    the flagged pattern is deliberate (e.g., @since tags, 'by design' comments,
    dev-only fallbacks).
    """
    lines = content.split("\n")

    # Look at a generous window around the reported line
    if line_number and 0 < line_number <= len(lines):
        window_start = max(0, line_number - 10)
        window_end = min(len(lines), line_number + 5)
    else:
        # No line number — check the whole file (first 50 lines)
        window_start = 0
        window_end = min(len(lines), 50)

    window_text = "\n".join(lines[window_start:window_end])

    # Check for intent comments
    for pattern in _INTENT_COMMENT_PATTERNS:
        match = pattern.search(window_text)
        if match:
            return f"Developer intent detected: '{match.group(0).strip()}' near line {line_number or '?'}"

    # For hardcoded secret / misconfiguration categories, check for dev-only fallbacks
    # Use the FULL file for fallback detection since env vars may be far from the constant
    norm_cat = category.lower().replace(" ", "_").replace("-", "_")
    if norm_cat in ("hardcoded_secret", "hardcoded_secrets", "security_misconfiguration", "misconfiguration"):
        full_text = content  # Check the whole file, not just the window
        for pattern in _DEV_FALLBACK_PATTERNS:
            match = pattern.search(full_text)
            if match:
                return f"Dev-only fallback pattern: '{match.group(0).strip()}' — not a production secret"

    return None


def _is_comment_line(line: str, file_path: str) -> bool:
    """Check if a line is a comment."""
    stripped = line.strip()
    if file_path.endswith(".py"):
        return bool(_PY_LINE_COMMENT.match(stripped)) or bool(_PY_DOCSTRING.match(stripped))
    return bool(_JS_LINE_COMMENT.match(stripped))


def _is_in_block_comment(lines: list[str], line_number: int) -> bool:
    """Check if a 1-based line sits inside a JS-style /* ... */ block comment.

    Tracks block-comment state across the lines preceding the target, and
    also treats a target line that itself opens a block comment as commented.
    """
    in_block = False
    for raw in lines[: line_number - 1]:
        i = 0
        while i < len(raw):
            if in_block:
                end = raw.find("*/", i)
                if end == -1:
                    break
                in_block = False
                i = end + 2
            else:
                start = raw.find("/*", i)
                if start == -1:
                    break
                in_block = True
                i = start + 2
    if in_block:
        return True
    if 0 < line_number <= len(lines):
        target = lines[line_number - 1]
        # Strip line-number prefix if present (from read_file format)
        if "\t" in target:
            target = target.split("\t", 1)[1]
        return bool(_JS_BLOCK_COMMENT_START.match(target.strip()))
    return False


def _line_has_sink_pattern(line: str, category: str) -> bool:
    """Check if a line contains a sink pattern for the given vuln category."""
    normalized_cat = category.lower().replace(" ", "_").replace("-", "_")
    patterns = _CATEGORY_SINK_PATTERNS.get(normalized_cat, [])
    return any(p.search(line) for p in patterns)


def _normalize_finding_category(cat: str) -> str:
    """Normalize to our canonical form for lookup."""
    return cat.lower().strip().replace(" ", "_").replace("-", "_")


# =========================================================================
# Layer A: Basic deterministic checks
# =========================================================================

def validate_finding_basic(finding: dict, fs_tools) -> tuple[bool, str]:
    """Run basic deterministic checks on a finding.

    Returns (is_valid, reason) where reason explains rejection.
    """
    file_path = finding.get("file_path", "")
    line_number = finding.get("line_number")
    category = _normalize_finding_category(finding.get("category", ""))

    # Check 1: File must exist
    if file_path:
        result = fs_tools.read_file(file_path)
        if "error" in result:
            return False, f"File does not exist: {file_path}"

        content = result.get("content", "")
        lines = content.split("\n")

        # Check 2: If line number provided, verify it has something relevant
        if line_number and 0 < line_number <= len(lines):
            target_line = lines[line_number - 1]
            # Strip line-number prefix if present (from read_file format)
            if "\t" in target_line:
                target_line = target_line.split("\t", 1)[1]

            # Check 2a: Is this line a comment?
            if _is_comment_line(target_line, file_path):
                return False, f"Reported line {line_number} is a comment"
            if not file_path.endswith(".py") and _is_in_block_comment(lines, line_number):
                return False, f"Reported line {line_number} is inside a block comment"

            # Check 2b: Does the line contain anything related to the claimed category?
            # Only reject if we have specific sink patterns AND the line is clearly unrelated
            # Use a generous window: check line +-3
            window_start = max(0, line_number - 4)
            window_end = min(len(lines), line_number + 3)
            window_lines = lines[window_start:window_end]
            window_text = "\n".join(
                l.split("\t", 1)[1] if "\t" in l else l for l in window_lines
            )

            # Skip sink check for categories where the vulnerability is commonly
            # indirect (input stored in one file, sink in another file).
            # SSRF: URL stored in DB by controller, fetched by background service
            # Auth bypass: middleware misconfigured in one file, exploited via another
            # Data exposure: data returned by helper, exposed by controller
            _INDIRECT_CATEGORIES = {
                "ssrf", "auth_bypass", "authentication_bypass", "authorization_bypass",
                "data_exposure", "idor", "business_logic_flaw",
            }
            if category in _CATEGORY_SINK_PATTERNS and category not in _INDIRECT_CATEGORIES:
                if not any(p.search(window_text) for p in _CATEGORY_SINK_PATTERNS[category]):
                    return False, (
                        f"Line {line_number} (±3 lines) has no {category} sink pattern"
                    )

    # Check 3: Non-production path — reject findings in test/CLI/docs/examples dirs
    if _is_non_production_path(file_path):
        return False, f"File is in a non-production path (test/CLI/docs/examples): {file_path}"

    # Check 4: Developer intent — reject findings where code shows deliberate design
    if file_path:
        result = fs_tools.read_file(file_path)
        if "error" not in result:
            file_content = result.get("content", "")
            intent_reason = _check_developer_intent(file_content, line_number, category)
            if intent_reason:
                return False, f"Intentional design: {intent_reason}"

    return True, "passed"


# =========================================================================
# Layer B: Tree-sitter sink verification
# =========================================================================

_TS_LANGUAGES: dict = {}  # Lazy-loaded


def _get_ts_language(file_path: str):
    """Get the appropriate tree-sitter language for a file."""
    global _TS_LANGUAGES

    ext = os.path.splitext(file_path)[1].lower()

    lang_key = None
    if ext in (".js", ".jsx", ".mjs"):
        lang_key = "javascript"
    elif ext in (".ts", ".tsx", ".mts"):
        lang_key = "typescript"
    elif ext == ".py":
        lang_key = "python"

    if lang_key is None:
        return None

    if lang_key not in _TS_LANGUAGES:
        try:
            import tree_sitter as ts
            if lang_key == "javascript":
                import tree_sitter_javascript as ts_js
                _TS_LANGUAGES["javascript"] = ts.Language(ts_js.language())
            elif lang_key == "typescript":
                import tree_sitter_typescript as ts_ts
                _TS_LANGUAGES["typescript"] = ts.Language(ts_ts.language_typescript())
            elif lang_key == "python":
                import tree_sitter_python as ts_py
                _TS_LANGUAGES["python"] = ts.Language(ts_py.language())
        except Exception as e:
            logger.debug(f"tree-sitter language load failed for {lang_key}: {e}")
            _TS_LANGUAGES[lang_key] = None

    return _TS_LANGUAGES.get(lang_key)


def _find_enclosing_function(tree, line: int, language_key: str):
    """Find the AST node for the function enclosing the given line (0-indexed)."""
    if language_key == "python":
        func_types = {"function_definition"}
    else:
        func_types = {
            "function_declaration", "arrow_function", "function",
            "method_definition", "function_expression",
        }

    best = None
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type in func_types:
            if node.start_point.row <= line <= node.end_point.row:
                # Pick the tightest enclosing function
                if best is None or (node.start_point.row >= best.start_point.row):
                    best = node
        for child in node.children:
            if child.start_point.row <= line <= child.end_point.row + 5:
                stack.append(child)
            elif child.start_point.row > line + 5:
                break

    return best


def _extract_parameter_names(func_node, language_key: str) -> set[str]:
    """Extract parameter names from a function node."""
    params = set()

    if language_key == "python":
        # Python: def foo(request, pk, **kwargs)
        for child in func_node.children:
            if child.type == "parameters":
                for param in child.children:
                    if param.type == "identifier":
                        params.add(param.text.decode())
                    elif param.type in ("default_parameter", "typed_parameter", "typed_default_parameter"):
                        for sub in param.children:
                            if sub.type == "identifier":
                                params.add(sub.text.decode())
                                break
                    elif param.type in ("list_splat_pattern", "dictionary_splat_pattern"):
                        for sub in param.children:
                            if sub.type == "identifier":
                                params.add(sub.text.decode())
    else:
        # JS/TS: function foo(req, res) or (req, res) =>
        for child in func_node.children:
            if child.type == "formal_parameters":
                for param in child.children:
                    if param.type == "identifier":
                        params.add(param.text.decode())
                    elif param.type in ("required_parameter", "optional_parameter"):
                        for sub in param.children:
                            if sub.type == "identifier":
                                params.add(sub.text.decode())
                                break
                    elif param.type == "object_pattern":
                        # Destructured: ({ body, params })
                        for sub in param.children:
                            if sub.type == "shorthand_property_identifier_pattern":
                                params.add(sub.text.decode())
                            elif sub.type == "pair_pattern":
                                for kv in sub.children:
                                    if kv.type == "property_identifier":
                                        params.add(kv.text.decode())

    return params


_INPUT_ACCESSOR_RE = re.compile(
    r"\b(?:req|request|params|query|body|searchParams|self\.request)"
    r"(?:\.\w+)*"
    r"|(?:request\.(?:GET|POST|FILES|body|json|form|args|data|query_params))"
    r"|(?:req\.(?:body|query|params|cookies|headers))"
    r"|await\s+\w+\.json\(\)"
    r"|getattr\s*\(\s*request",
    re.I,
)

_KEYWORDS = {"const", "let", "var", "return", "await", "async", "function",
             "if", "else", "for", "while", "true", "false", "null", "undefined",
             "def", "class", "import", "from", "None", "True", "False", "self",
             "new", "typeof", "instanceof", "try", "catch", "throw", "export"}


def _function_has_user_input_at_sink(func_node, sink_line: int, language_key: str) -> bool:
    """Check if user input can plausibly reach the sink line.

    Strategy:
    1. Extract function parameters
    2. Only consider params that match known user-input names (req, request, body, etc.)
    3. Check for direct user-input accessor patterns near the sink
    4. Trace variables backwards: if a variable on the sink line was assigned
       from user input earlier in the function, it counts

    If only non-input params (config, options, settings) appear, reject.
    """
    func_text = func_node.text.decode()
    func_start_line = func_node.start_point.row
    func_lines = func_text.split("\n")

    # Get parameter names
    param_names = _extract_parameter_names(func_node, language_key)

    # Only consider params that look like user input sources
    input_params = param_names & _USER_INPUT_NAMES

    # Sink window: ±2 lines around the reported line
    relative_sink = sink_line - func_start_line
    check_start = max(0, relative_sink - 2)
    check_end = min(len(func_lines), relative_sink + 3)
    sink_window = " ".join(func_lines[check_start:check_end])

    # Check 1: Does the sink window directly reference a known input parameter?
    for identifier in input_params:
        if re.search(rf"\b{re.escape(identifier)}\b", sink_window):
            return True

    # Check 2: Does the sink window contain a user-input accessor pattern?
    if _INPUT_ACCESSOR_RE.search(sink_window):
        return True

    # Check 3: Trace backwards — does any variable in the sink window
    # get assigned from user input earlier in the function?
    sink_identifiers = set(re.findall(r"\b([a-zA-Z_]\w*)\b", sink_window)) - _KEYWORDS

    pre_sink_lines = func_lines[:max(0, relative_sink)]
    pre_sink_text = "\n".join(pre_sink_lines)

    for ident in sink_identifiers:
        assign_pattern = re.compile(
            rf"(?:const|let|var)?\s*{re.escape(ident)}\s*=\s*(.*)",
        )
        for match in assign_pattern.finditer(pre_sink_text):
            rhs = match.group(1)
            if _INPUT_ACCESSOR_RE.search(rhs):
                return True
            for ip in input_params:
                if re.search(rf"\b{re.escape(ip)}\b", rhs):
                    return True

    return False


def validate_finding_treesitter(finding: dict, fs_tools) -> tuple[bool, str]:
    """Tree-sitter based sink verification.

    Parses the function containing the finding, checks if user input
    can plausibly reach the dangerous sink.

    Returns (is_valid, reason).
    """
    file_path = finding.get("file_path", "")
    line_number = finding.get("line_number")
    category = _normalize_finding_category(finding.get("category", ""))

    # Only run for categories where we have sink patterns
    if category not in _CATEGORY_SINK_PATTERNS:
        return True, "no sink patterns for category"

    if not file_path or not line_number:
        return True, "no file/line to verify"

    ts_lang = _get_ts_language(file_path)
    if ts_lang is None:
        return True, "unsupported language for tree-sitter"

    # Read the file
    result = fs_tools.read_file(file_path)
    if "error" in result:
        return True, "could not read file"

    raw_content = result.get("content", "")
    # Strip line-number prefixes
    lines = raw_content.split("\n")
    clean_lines = []
    for line in lines:
        if "\t" in line:
            clean_lines.append(line.split("\t", 1)[1])
        else:
            clean_lines.append(line)
    source_bytes = "\n".join(clean_lines).encode("utf-8")

    try:
        import tree_sitter as ts
        parser = ts.Parser(ts_lang)
        tree = parser.parse(source_bytes)
    except Exception as e:
        logger.debug(f"tree-sitter parse failed for {file_path}: {e}")
        return True, "parse failed"

    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".py":
        lang_key = "python"
    elif ext in (".ts", ".tsx", ".mts"):
        lang_key = "typescript"
    else:
        lang_key = "javascript"

    # Find enclosing function (0-indexed line)
    func_node = _find_enclosing_function(tree, line_number - 1, lang_key)
    if func_node is None:
        # No enclosing function — might be module-level code, allow it
        return True, "no enclosing function found"

    # Check if user input identifiers appear near the sink
    has_input = _function_has_user_input_at_sink(func_node, line_number - 1, lang_key)
    if not has_input:
        return False, (
            f"tree-sitter: no user input identifiers found near sink at line {line_number} "
            f"in function {func_node.children[1].text.decode() if len(func_node.children) > 1 else '?'}"
        )

    return True, "user input may reach sink"


# =========================================================================
# Main entry point
# =========================================================================

def run_static_validation(
    potential_findings: list[dict],
    fs_tools,
    enable_treesitter: bool = True,
) -> tuple[list[dict], list[dict], dict]:
    """Run deterministic pre-validation on all hunter findings.

    Args:
        potential_findings: Raw findings from hunter swarm
        fs_tools: FileSystemTools instance for file access
        enable_treesitter: Whether to run tree-sitter checks (Layer B)

    Returns:
        (passed, rejected, stats) where:
        - passed: findings that survived validation (indices preserved)
        - rejected: findings that were filtered out
        - stats: validation statistics
    """
    stats = {
        "input_count": len(potential_findings),
        "basic_rejected": 0,
        "treesitter_rejected": 0,
        "passed": 0,
        "basic_rejections": [],
        "treesitter_rejections": [],
    }

    passed: list[dict] = []
    rejected: list[dict] = []

    for i, finding in enumerate(potential_findings):
        finding["_original_index"] = i

        # Layer A: Basic checks
        is_valid, reason = validate_finding_basic(finding, fs_tools)
        if not is_valid:
            finding["_rejection_reason"] = f"basic: {reason}"
            rejected.append(finding)
            stats["basic_rejected"] += 1
            stats["basic_rejections"].append({
                "index": i,
                "file": finding.get("file_path", ""),
                "category": finding.get("category", ""),
                "reason": reason,
            })
            continue

        # Layer B: Tree-sitter verification
        if enable_treesitter:
            is_valid, reason = validate_finding_treesitter(finding, fs_tools)
            if not is_valid:
                finding["_rejection_reason"] = f"treesitter: {reason}"
                rejected.append(finding)
                stats["treesitter_rejected"] += 1
                stats["treesitter_rejections"].append({
                    "index": i,
                    "file": finding.get("file_path", ""),
                    "category": finding.get("category", ""),
                    "reason": reason,
                })
                continue

        passed.append(finding)
        stats["passed"] += 1

    total_rejected = stats["basic_rejected"] + stats["treesitter_rejected"]
    logger.info(
        f"Static validation: {stats['input_count']} findings → "
        f"{stats['passed']} passed, {total_rejected} rejected "
        f"(basic: {stats['basic_rejected']}, tree-sitter: {stats['treesitter_rejected']})"
    )

    return passed, rejected, stats
