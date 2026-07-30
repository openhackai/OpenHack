"""
Browser verifier agent.

Takes a confirmed finding and drives a real Chromium browser to
verify the exploit, capturing screenshot evidence along the way.
Handles login flows, CSRF tokens, and multi-step UI interactions.
"""

import json
import logging
from typing import Optional

from .base import BaseAgent
from .llm import Message, ToolResult
from ..browser.runner import BrowserRunner, BrowserContext
from openhack.prompts import format_project_context
from openhack.prompts.browser_verifier import (
    BROWSER_VERIFIER_PROMPT,
    BROWSER_VERIFIER_TOOL_INSTRUCTIONS,
)

logger = logging.getLogger(__name__)


# ── Tool definitions for the browser verifier ────────────────────

BROWSER_NAVIGATE_TOOL = {
    "name": "browser_navigate",
    "description": (
        "Navigate the browser to a URL. Returns the page title, URL, "
        "and text content after navigation completes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "URL or path to navigate to (e.g., /login, /api/users). Paths are prefixed with the sandbox base URL.",
            },
            "wait_until": {
                "type": "string",
                "enum": ["load", "domcontentloaded", "networkidle"],
                "description": "When to consider navigation complete (default: networkidle)",
                "default": "networkidle",
            },
        },
        "required": ["url"],
    },
}

BROWSER_CLICK_TOOL = {
    "name": "browser_click",
    "description": (
        "Click an element on the page. Returns the updated page state after the click. "
        "Use selector_type to choose how to find the element."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "selector": {
                "type": "string",
                "description": "The selector to find the element (CSS selector, visible text, or ARIA role)",
            },
            "selector_type": {
                "type": "string",
                "enum": ["css", "text", "role"],
                "description": "How to interpret the selector (default: css)",
                "default": "css",
            },
        },
        "required": ["selector"],
    },
}

BROWSER_FILL_TOOL = {
    "name": "browser_fill",
    "description": (
        "Type text into a form field. Clears the field first, then types the value. "
        "Use CSS selectors to identify the input field."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "selector": {
                "type": "string",
                "description": "CSS selector for the input field (e.g., input[name='email'], #password)",
            },
            "value": {
                "type": "string",
                "description": "The text to type into the field",
            },
        },
        "required": ["selector", "value"],
    },
}

BROWSER_SCREENSHOT_TOOL = {
    "name": "browser_screenshot",
    "description": (
        "Take a screenshot of the current page. Screenshots are saved as evidence. "
        "Use descriptive names like 'login_page', 'after_xss_injection', 'exploit_confirmed'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Descriptive name for the screenshot (used in filename)",
            },
        },
        "required": ["name"],
    },
}

BROWSER_GET_CONTENT_TOOL = {
    "name": "browser_get_content",
    "description": (
        "Read the page content — either the full page or a specific element. "
        "Use format 'html' to see raw HTML (useful for checking if XSS payloads are unescaped), "
        "or 'text' for readable text content."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "selector": {
                "type": "string",
                "description": "CSS selector for a specific element (omit for full page)",
            },
            "format": {
                "type": "string",
                "enum": ["text", "html"],
                "description": "Output format: 'text' for readable content, 'html' for raw HTML (default: text)",
                "default": "text",
            },
        },
    },
}

BROWSER_EXECUTE_JS_TOOL = {
    "name": "browser_execute_js",
    "description": (
        "Execute JavaScript in the page context. Use this to inspect the DOM, "
        "check for XSS payload execution, read localStorage, or interact with "
        "the page programmatically. Returns the evaluation result."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "script": {
                "type": "string",
                "description": "JavaScript code to execute (e.g., 'document.title', 'document.cookie', 'document.querySelector(\"#secret\").textContent')",
            },
        },
        "required": ["script"],
    },
}

BROWSER_WAIT_FOR_TOOL = {
    "name": "browser_wait_for",
    "description": (
        "Wait for an element to appear, disappear, or reach a specific state. "
        "Useful after navigation or form submission when content loads asynchronously."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "selector": {
                "type": "string",
                "description": "CSS selector to wait for",
            },
            "timeout_ms": {
                "type": "integer",
                "description": "Max time to wait in milliseconds (default: 5000)",
                "default": 5000,
            },
            "state": {
                "type": "string",
                "enum": ["visible", "hidden", "attached", "detached"],
                "description": "What state to wait for (default: visible)",
                "default": "visible",
            },
        },
        "required": ["selector"],
    },
}

BROWSER_GET_COOKIES_TOOL = {
    "name": "browser_get_cookies",
    "description": (
        "Get all cookies for the current page. Useful for inspecting session cookies, "
        "checking HttpOnly/Secure/SameSite flags, and verifying authentication state."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
    },
}

BROWSER_SNAPSHOT_TOOL = {
    "name": "browser_snapshot",
    "description": (
        "Tag every interactive element on the current page with a stable @eN ref "
        "and return a compact map. ALWAYS call this BEFORE click/fill — it eliminates "
        "the need to guess CSS selectors. Returns lines like:\n"
        "  @e1 <button name='submit'> \"Sign In\"\n"
        "  @e2 <input type='email' name='email'>\n"
        "Then use the ref directly: browser_fill(selector='@e2', value='...') "
        "or browser_click(selector='@e1'). Refs persist until the next snapshot or navigation."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
    },
}

REPORT_BROWSER_RESULT_TOOL = {
    "name": "report_browser_result",
    "description": (
        "Report the final result of your browser-based exploit verification. "
        "Call this when you have either confirmed the exploit works "
        "or determined it is not exploitable after multiple attempts."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["exploitable", "not_exploitable"],
                "description": "Whether the vulnerability was successfully exploited",
            },
            "confidence": {
                "type": "string",
                "enum": ["high", "medium", "low"],
                "description": "Confidence in the result",
            },
            "evidence": {
                "type": "string",
                "description": "Description of what you observed — response data, DOM state, behavior proving exploitation or explaining failure",
            },
            "attempts_made": {
                "type": "integer",
                "description": "How many exploit attempts were made",
            },
            "screenshots": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of screenshot filenames captured as evidence",
            },
            "dom_evidence": {
                "type": "string",
                "description": "Relevant HTML/DOM snippets proving the exploit (for XSS, injection, etc.)",
            },
            "console_evidence": {
                "type": "string",
                "description": "Relevant browser console output",
            },
            "network_evidence": {
                "type": "string",
                "description": "Relevant network activity (redirects, requests to attacker URLs, etc.)",
            },
            "reason": {
                "type": "string",
                "description": "For not_exploitable: why the exploit cannot work in practice",
            },
        },
        "required": ["status", "confidence", "evidence", "attempts_made"],
    },
}


BROWSER_TOOLS = [
    BROWSER_NAVIGATE_TOOL,
    BROWSER_SNAPSHOT_TOOL,
    BROWSER_CLICK_TOOL,
    BROWSER_FILL_TOOL,
    BROWSER_SCREENSHOT_TOOL,
    BROWSER_GET_CONTENT_TOOL,
    BROWSER_EXECUTE_JS_TOOL,
    BROWSER_WAIT_FOR_TOOL,
    BROWSER_GET_COOKIES_TOOL,
    REPORT_BROWSER_RESULT_TOOL,
]


class BrowserVerifierAgent(BaseAgent):
    """Agent that verifies vulnerabilities using a real browser."""

    name = "browser_verifier"
    description = "Verifying exploit in browser"

    def __init__(
        self,
        *args,
        sandbox_url: str = "",
        browser_runner: Optional[BrowserRunner] = None,
        sandbox_orchestrator=None,
        finding_index: int = 0,
        max_attempts: int = 7,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.sandbox_url = sandbox_url
        self.browser_runner = browser_runner
        self.sandbox_orchestrator = sandbox_orchestrator
        self.finding_index = finding_index
        self.max_attempts = max_attempts
        self.browser_result: Optional[dict] = None
        self.attempt_count = 0
        self._browser_ctx: Optional[BrowserContext] = None

        self.name = f"browser_verifier:finding_{finding_index}"
        self.description = f"Browser-verifying finding {finding_index}"

    def get_system_prompt(self, context: dict) -> str:
        finding = context.get("finding", {})
        project_context = context.get("project_context", {})
        project_context_str = format_project_context(project_context)

        finding_details = f"""
### Vulnerability: {finding.get('category', 'Unknown')}
- **Severity**: {finding.get('severity', 'Unknown')}
- **File**: {finding.get('file_path', 'Unknown')}
- **Line**: {finding.get('line_number', 'Unknown')}
- **Description**: {finding.get('description', 'No description')}
- **Code**:
```
{finding.get('code_snippet', 'No code snippet')}
```
- **Original PoC**:
```
{finding.get('poc', 'No PoC provided')}
```
- **Confidence**: {finding.get('confidence', 'Unknown')}
- **CVSS Score**: {finding.get('cvss_score', 'N/A')}
"""

        prompt = BROWSER_VERIFIER_PROMPT.format(
            project_context=project_context_str,
            sandbox_url=self.sandbox_url,
            finding_details=finding_details,
            max_attempts=self.max_attempts,
        )
        prompt += BROWSER_VERIFIER_TOOL_INSTRUCTIONS
        return prompt

    def get_tools(self) -> list[dict]:
        return super().get_tools() + BROWSER_TOOLS

    async def _handle_browser_navigate(self, args: dict) -> dict:
        self.attempt_count += 1
        result = await self.browser_runner.navigate(
            self._browser_ctx,
            url=args.get("url", "/"),
            wait_until=args.get("wait_until", "networkidle"),
        )
        return result.to_dict()

    async def _handle_browser_click(self, args: dict) -> dict:
        result = await self.browser_runner.click(
            self._browser_ctx,
            selector=args.get("selector", ""),
            selector_type=args.get("selector_type", "css"),
        )
        return result.to_dict()

    async def _handle_browser_fill(self, args: dict) -> dict:
        result = await self.browser_runner.fill(
            self._browser_ctx,
            selector=args.get("selector", ""),
            value=args.get("value", ""),
        )
        return result.to_dict()

    async def _handle_browser_screenshot(self, args: dict) -> dict:
        return await self.browser_runner.screenshot(
            self._browser_ctx,
            name=args.get("name", "screenshot"),
        )

    async def _handle_browser_get_content(self, args: dict) -> dict:
        return await self.browser_runner.get_content(
            self._browser_ctx,
            selector=args.get("selector"),
            fmt=args.get("format", "text"),
        )

    async def _handle_browser_execute_js(self, args: dict) -> dict:
        return await self.browser_runner.execute_js(
            self._browser_ctx,
            script=args.get("script", ""),
        )

    async def _handle_browser_wait_for(self, args: dict) -> dict:
        return await self.browser_runner.wait_for(
            self._browser_ctx,
            selector=args.get("selector", ""),
            timeout=args.get("timeout_ms", 5000),
            state=args.get("state", "visible"),
        )

    async def _handle_browser_get_cookies(self, args: dict) -> dict:
        return await self.browser_runner.get_cookies(self._browser_ctx)

    async def _handle_browser_snapshot(self, args: dict) -> dict:
        return await self.browser_runner.snapshot(self._browser_ctx)

    def _handle_report_browser_result(self, args: dict) -> dict:
        self.browser_result = {
            "finding_index": self.finding_index,
            "status": args.get("status", "not_exploitable"),
            "confidence": args.get("confidence", "medium"),
            "evidence": args.get("evidence", ""),
            "attempts_made": args.get("attempts_made", self.attempt_count),
            "screenshots": args.get("screenshots", []),
            "dom_evidence": args.get("dom_evidence"),
            "console_evidence": args.get("console_evidence"),
            "network_evidence": args.get("network_evidence"),
            "reason": args.get("reason"),
        }
        return {"status": "recorded", "finding_index": self.finding_index}

    async def run(self, task: str, context: Optional[dict] = None) -> dict:
        context = context or {}
        self.session.current_agent = self.name
        self.session.start_turn(task, self.name)
        self.browser_result = None
        self.attempt_count = 0

        self._browser_ctx = await self.browser_runner.create_context(self.finding_index)

        try:
            system_prompt = self.get_system_prompt(context)
            self.messages = []
            self._append_message(Message(role="user", content=task), source="run")
            self._seed_existing_instructions()

            max_iterations = self.max_attempts * 4
            iteration = 0

            while iteration < max_iterations:
                if self.session.cancelled:
                    break
                iteration += 1

                self._inject_pending_instructions()

                response = await self.llm.chat(
                    messages=self.messages, tools=self.get_tools(), system=system_prompt,
                )

                self._record_response_usage(response)

                if response.content:
                    self.session.add_trace(
                        agent=self.name, event_type="thinking", content=response.content,
                    )

                if not response.tool_calls:
                    self._append_message(
                        Message(role="assistant", content=response.content),
                        source="model_text_response",
                    )
                    result = self._build_result(response.content or "")
                    self.session.finish_turn("completed", result, agent=self.name)
                    return result

                assistant_msg = Message(
                    role="assistant", content=response.content,
                    tool_calls=[
                        {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)}}
                        for tc in response.tool_calls
                    ],
                )
                self._append_message(assistant_msg, source="model_tool_response")

                should_finish = False
                for tool_call in response.tool_calls:
                    self.session.add_trace(
                        agent=self.name, event_type="tool_call",
                        content=f"Calling {tool_call.name}",
                        tool_name=tool_call.name, tool_input=tool_call.arguments,
                        model_call_id=response.model_call_id,
                        tool_call_id=tool_call.id,
                        metadata={
                            "raw_arguments": tool_call.raw_arguments,
                            "parse_error": tool_call.parse_error,
                        },
                    )

                    try:
                        if tool_call.name == "browser_navigate":
                            result = await self._handle_browser_navigate(tool_call.arguments)
                        elif tool_call.name == "browser_click":
                            result = await self._handle_browser_click(tool_call.arguments)
                        elif tool_call.name == "browser_fill":
                            result = await self._handle_browser_fill(tool_call.arguments)
                        elif tool_call.name == "browser_screenshot":
                            result = await self._handle_browser_screenshot(tool_call.arguments)
                        elif tool_call.name == "browser_get_content":
                            result = await self._handle_browser_get_content(tool_call.arguments)
                        elif tool_call.name == "browser_execute_js":
                            result = await self._handle_browser_execute_js(tool_call.arguments)
                        elif tool_call.name == "browser_wait_for":
                            result = await self._handle_browser_wait_for(tool_call.arguments)
                        elif tool_call.name == "browser_get_cookies":
                            result = await self._handle_browser_get_cookies(tool_call.arguments)
                        elif tool_call.name == "browser_snapshot":
                            result = await self._handle_browser_snapshot(tool_call.arguments)
                        elif tool_call.name == "report_browser_result":
                            result = self._handle_report_browser_result(tool_call.arguments)
                            should_finish = True
                        else:
                            result = self.tools.execute_tool(tool_call.name, tool_call.arguments)
                    except Exception as e:
                        result = {"error": f"Tool execution failed: {str(e)}"}

                    self.session.add_trace(
                        agent=self.name, event_type="tool_result",
                        content=f"Result from {tool_call.name}",
                        tool_name=tool_call.name, tool_output=result,
                        model_call_id=response.model_call_id,
                        tool_call_id=tool_call.id,
                    )

                    raw_content = json.dumps(result) if isinstance(result, dict) else str(result)
                    truncated_content = self.context_manager.truncate_tool_result(tool_call.name, raw_content)
                    tool_result = ToolResult(tool_call_id=tool_call.id, content=truncated_content)
                    self._append_message(
                        tool_result.to_message(),
                        source=f"tool_result:{tool_call.name}",
                    )

                if should_finish:
                    result = self._build_result(response.content or "")
                    self.session.finish_turn("completed", result, agent=self.name)
                    return result

                if self.context_manager.needs_compaction():
                    self.messages = self.context_manager.compact_messages(self.messages)
                    logger.info(f"[{self.name}] Compacted message history")

            if not self.browser_result:
                self.browser_result = self._infer_result_from_trace()

            result = self._build_result("Max iterations reached")
            self.session.finish_turn("max_iterations", result, agent=self.name)
            return result

        finally:
            if self._browser_ctx:
                await self._browser_ctx.close()

    def _infer_result_from_trace(self) -> dict:
        """Infer the verdict from the trace when the agent ran out of iterations
        without calling report_browser_result.

        Looks for evidence patterns that strongly indicate exploitation:
        - browser_fill with payload-looking content
        - browser_click after the fill (form submission)
        - success message in subsequent page content (Saved/Updated/Created/Successful)
        - screenshots taken at the right moments
        """
        my_trace = [
            t for t in self.session.trace
            if t.agent == self.name and t.event_type in ("tool_call", "tool_result")
        ]

        # Walk the trace and gather signals
        payload_substrings = self._payload_signatures()
        evidence: list[str] = []
        screenshots: list[str] = []
        fill_with_payload = False
        click_after_payload_fill = False
        success_after_payload = False
        navigated_to_external = False
        last_fill_was_payload = False

        for entry in my_trace:
            tool = entry.tool_name or ""
            args = entry.tool_input or {}
            out = entry.tool_output or {}

            if tool == "browser_fill" and entry.event_type == "tool_call":
                value = str(args.get("value", ""))
                if any(sig in value for sig in payload_substrings):
                    fill_with_payload = True
                    last_fill_was_payload = True
                    evidence.append(f"browser_fill with payload content: {value[:120]}")
                else:
                    last_fill_was_payload = False

            elif tool == "browser_click" and entry.event_type == "tool_call":
                if last_fill_was_payload:
                    click_after_payload_fill = True
                    evidence.append(f"browser_click after payload fill: {args.get('selector', '')}")

            elif tool == "browser_navigate" and entry.event_type == "tool_call":
                url = str(args.get("url", ""))
                if url.startswith("http") and ("evil" in url or "wikipedia.org" in url
                                               or "example.com" in url or "google.com" in url
                                               or "169.254.169.254" in url):
                    navigated_to_external = True
                    evidence.append(f"browser_navigate to external/SSRF target: {url}")

            elif tool == "browser_screenshot" and entry.event_type == "tool_call":
                screenshots.append(str(args.get("name", "")))

            elif tool in ("browser_navigate", "browser_click", "browser_get_content",
                          "browser_snapshot") and entry.event_type == "tool_result":
                if isinstance(out, dict):
                    haystack = (str(out.get("page_content", "")) + " "
                                + str(out.get("snapshot", "")) + " "
                                + str(out.get("content", "")) + " "
                                + str(out.get("page_title", ""))).lower()
                    success_markers = (
                        "successfully", "updated successfully", "saved successfully",
                        "created successfully", "profile updated", "successful",
                        "logged in", "welcome back", "dashboard",
                    )
                    if fill_with_payload and any(m in haystack for m in success_markers):
                        success_after_payload = True
                        marker = next(m for m in success_markers if m in haystack)
                        evidence.append(f"page showed success marker after payload submission: '{marker}'")

            # Check for redirect chain to attacker-controlled URL
            if (tool == "browser_navigate" and entry.event_type == "tool_result"
                    and isinstance(out, dict)):
                page_url = str(out.get("page_url", ""))
                if page_url and not page_url.startswith(self.sandbox_url):
                    if "wikipedia.org" in page_url or "evil" in page_url or "google.com" in page_url:
                        navigated_to_external = True
                        evidence.append(f"page navigated to external URL after redirect: {page_url}")

        # Decision rules
        if (fill_with_payload and click_after_payload_fill and success_after_payload):
            return {
                "finding_index": self.finding_index,
                "status": "exploitable",
                "confidence": "medium",
                "evidence": (
                    "Inferred from trace (agent exhausted iteration budget before reporting). "
                    + " | ".join(evidence[:8])
                ),
                "attempts_made": self.attempt_count,
                "screenshots": screenshots,
                "reason": "Trace shows payload was submitted and the app accepted it (success message observed). "
                          "DOM-level execution was not separately confirmed.",
                "inferred_from_trace": True,
            }

        if navigated_to_external:
            return {
                "finding_index": self.finding_index,
                "status": "exploitable",
                "confidence": "medium",
                "evidence": (
                    "Inferred from trace (open-redirect-style evidence). "
                    + " | ".join(evidence[:8])
                ),
                "attempts_made": self.attempt_count,
                "screenshots": screenshots,
                "reason": "Trace shows the browser was redirected to an external/attacker-controlled URL.",
                "inferred_from_trace": True,
            }

        return {
            "finding_index": self.finding_index,
            "status": "not_exploitable",
            "confidence": "low",
            "evidence": "Max iterations reached without confirming exploit. Trace did not contain payload-submission + success-marker pattern.",
            "attempts_made": self.attempt_count,
            "screenshots": screenshots,
            "reason": "Agent exhausted iteration budget without conclusive evidence in trace.",
            "inferred_from_trace": True,
        }

    def _payload_signatures(self) -> list[str]:
        """Substrings that strongly suggest a fill value is an exploit payload."""
        return [
            "<script", "</script>", "onerror=", "onload=", "onclick=",
            "javascript:", "<img src=x", "<svg",
            "' OR '", "' or '", '" OR "', "1=1", "UNION SELECT", "union select",
            "../", "..\\", "/etc/passwd", "/proc/self",
            "127.0.0.1", "169.254.169.254", "localhost:",
            "${", "{{", "<%",
            "; cat ", "| cat ", "$(", "`",
        ]

    def _build_result(self, summary: str) -> dict:
        return {
            "raw_output": summary,
            "browser_result": self.browser_result,
            "attempts_made": self.attempt_count,
            "type": "browser_verification_complete",
        }

    def _parse_final_response(self, content: str) -> dict:
        return self._build_result(content)
