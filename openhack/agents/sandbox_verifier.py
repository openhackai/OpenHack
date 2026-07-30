"""
Sandbox verifier agent.

Takes a confirmed finding and iteratively develops a working exploit
by executing requests against a live sandboxed instance of the target app.
The agent adapts its approach based on responses, trying multiple strategies
before concluding whether a vulnerability is exploitable.
"""

import json
import logging
from typing import Optional

from .base import BaseAgent
from .llm import Message, ToolResult
from ..sandbox.runner import ExploitRunner, ExploitResult
from openhack.prompts import SANDBOX_VERIFIER_PROMPT, SANDBOX_VERIFIER_TOOL_INSTRUCTIONS
from openhack.prompts import format_project_context

logger = logging.getLogger(__name__)


# ── Tool definitions for the sandbox verifier ──────────────────────

SANDBOX_HTTP_REQUEST_TOOL = {
    "name": "sandbox_http_request",
    "description": (
        "Execute an HTTP request against the live sandboxed application. "
        "Use this to test exploit payloads. Returns full response including "
        "status code, headers, and body."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "method": {
                "type": "string",
                "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
                "description": "HTTP method",
            },
            "path": {
                "type": "string",
                "description": "URL path (e.g., /api/users/1). Will be prefixed with the sandbox base URL.",
            },
            "headers": {
                "type": "object",
                "description": "HTTP headers as key-value pairs",
                "additionalProperties": {"type": "string"},
            },
            "body": {
                "type": "string",
                "description": "Raw request body (for form data, XML, etc.)",
            },
            "json_body": {
                "type": "object",
                "description": "JSON request body (automatically sets Content-Type: application/json)",
            },
            "follow_redirects": {
                "type": "boolean",
                "description": "Whether to follow HTTP redirects (default: false)",
                "default": False,
            },
        },
        "required": ["method", "path"],
    },
}

SANDBOX_MULTI_STEP_TOOL = {
    "name": "sandbox_multi_step",
    "description": (
        "Execute a chain of HTTP requests for multi-step exploits. "
        "Use this for exploits that need setup steps (e.g., register user → login → exploit). "
        "Each step's response body is available to later steps via {step_N_body} placeholders."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "method": {"type": "string"},
                        "path": {"type": "string"},
                        "headers": {"type": "object", "additionalProperties": {"type": "string"}},
                        "body": {"type": "string"},
                        "json_body": {"type": "object"},
                        "follow_redirects": {"type": "boolean"},
                    },
                    "required": ["method", "path"],
                },
                "description": "Ordered list of HTTP requests to execute",
            },
        },
        "required": ["steps"],
    },
}

SANDBOX_GET_LOGS_TOOL = {
    "name": "sandbox_get_logs",
    "description": (
        "Get the Docker container logs from the sandboxed application. "
        "Useful for debugging when requests fail unexpectedly or to see "
        "server-side errors triggered by your exploit attempts."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "tail": {
                "type": "integer",
                "description": "Number of log lines to return (default: 50)",
                "default": 50,
            },
        },
    },
}

REPORT_EXPLOIT_RESULT_TOOL = {
    "name": "report_exploit_result",
    "description": (
        "Report the final result of your exploit verification. "
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
            "working_poc": {
                "type": "string",
                "description": "The working Python exploit script (for exploitable findings)",
            },
            "evidence": {
                "type": "string",
                "description": "Evidence: response data proving exploitation, or explanation of why it failed",
            },
            "attempts_made": {
                "type": "integer",
                "description": "How many exploit attempts were made",
            },
            "exploit_request": {
                "type": "object",
                "description": "The exact HTTP request that worked (method, path, headers, body)",
            },
            "reason": {
                "type": "string",
                "description": "For not_exploitable: why the exploit cannot work in practice",
            },
        },
        "required": ["status", "confidence", "evidence", "attempts_made"],
    },
}


SANDBOX_TOOLS = [
    SANDBOX_HTTP_REQUEST_TOOL,
    SANDBOX_MULTI_STEP_TOOL,
    SANDBOX_GET_LOGS_TOOL,
    REPORT_EXPLOIT_RESULT_TOOL,
]


class SandboxVerifierAgent(BaseAgent):
    """Agent that verifies vulnerabilities by exploiting them in a sandbox."""

    name = "sandbox_verifier"
    description = "Verifying exploit in sandbox"

    def __init__(
        self,
        *args,
        sandbox_url: str = "",
        exploit_runner: Optional[ExploitRunner] = None,
        sandbox_orchestrator=None,
        finding_index: int = 0,
        max_attempts: int = 7,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.sandbox_url = sandbox_url
        self.exploit_runner = exploit_runner
        self.sandbox_orchestrator = sandbox_orchestrator
        self.finding_index = finding_index
        self.max_attempts = max_attempts
        self.exploit_result: Optional[dict] = None
        self.attempt_count = 0

        self.name = f"sandbox_verifier:finding_{finding_index}"
        self.description = f"Exploiting finding {finding_index} in sandbox"

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

        prompt = SANDBOX_VERIFIER_PROMPT.format(
            project_context=project_context_str,
            sandbox_url=self.sandbox_url,
            finding_details=finding_details,
            max_attempts=self.max_attempts,
        )
        prompt += SANDBOX_VERIFIER_TOOL_INSTRUCTIONS
        return prompt

    def get_tools(self) -> list[dict]:
        # Include filesystem tools (read_file, grep) plus sandbox tools
        return super().get_tools() + SANDBOX_TOOLS

    async def _handle_sandbox_http_request(self, args: dict) -> dict:
        """Execute a single HTTP request against the sandbox."""
        self.attempt_count += 1

        result = await self.exploit_runner.execute_request(
            method=args.get("method", "GET"),
            path=args.get("path", "/"),
            headers=args.get("headers"),
            body=args.get("body"),
            json_body=args.get("json_body"),
            follow_redirects=args.get("follow_redirects", False),
            attempt=self.attempt_count,
        )

        return result.to_dict()

    async def _handle_sandbox_multi_step(self, args: dict) -> dict:
        """Execute a multi-step exploit chain."""
        steps = args.get("steps", [])
        if not steps:
            return {"error": "No steps provided"}

        self.attempt_count += 1
        results = await self.exploit_runner.execute_multi_step(steps)

        return {
            "steps_executed": len(results),
            "results": [r.to_dict() for r in results],
        }

    async def _handle_sandbox_get_logs(self, args: dict) -> dict:
        """Get container logs."""
        if not self.sandbox_orchestrator:
            return {"error": "No sandbox orchestrator available"}

        tail = args.get("tail", 50)
        logs = await self.sandbox_orchestrator.get_logs(tail=tail)
        return {"logs": logs}

    def _handle_report_exploit_result(self, args: dict) -> dict:
        """Record the final exploit result."""
        self.exploit_result = {
            "finding_index": self.finding_index,
            "status": args.get("status", "not_exploitable"),
            "confidence": args.get("confidence", "medium"),
            "working_poc": args.get("working_poc"),
            "evidence": args.get("evidence", ""),
            "attempts_made": args.get("attempts_made", self.attempt_count),
            "exploit_request": args.get("exploit_request"),
            "reason": args.get("reason"),
        }
        return {"status": "recorded", "finding_index": self.finding_index}

    async def run(self, task: str, context: Optional[dict] = None) -> dict:
        context = context or {}
        self.session.current_agent = self.name
        self.session.start_turn(task, self.name)
        self.exploit_result = None
        self.attempt_count = 0

        system_prompt = self.get_system_prompt(context)
        self.messages = []
        self._append_message(Message(role="user", content=task), source="run")
        self._seed_existing_instructions()

        max_iterations = self.max_attempts * 4  # Allow multiple tool calls per attempt
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

                if tool_call.name == "sandbox_http_request":
                    result = await self._handle_sandbox_http_request(tool_call.arguments)
                elif tool_call.name == "sandbox_multi_step":
                    result = await self._handle_sandbox_multi_step(tool_call.arguments)
                elif tool_call.name == "sandbox_get_logs":
                    result = await self._handle_sandbox_get_logs(tool_call.arguments)
                elif tool_call.name == "report_exploit_result":
                    result = self._handle_report_exploit_result(tool_call.arguments)
                    should_finish = True
                else:
                    # Filesystem tools (read_file, grep, etc.)
                    result = self.tools.execute_tool(tool_call.name, tool_call.arguments)

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
                    tool_result.to_message(), source=f"tool_result:{tool_call.name}"
                )

            if should_finish:
                result = self._build_result(response.content or "")
                self.session.finish_turn("completed", result, agent=self.name)
                return result

            if self.context_manager.needs_compaction():
                self.messages = self.context_manager.compact_messages(self.messages)
                logger.info(f"[{self.name}] Compacted message history")

        # Max iterations reached without explicit report
        if not self.exploit_result:
            self.exploit_result = {
                "finding_index": self.finding_index,
                "status": "not_exploitable",
                "confidence": "low",
                "evidence": "Max iterations reached without confirming exploit",
                "attempts_made": self.attempt_count,
                "reason": "Agent exhausted iteration budget",
            }

        result = self._build_result("Max iterations reached")
        self.session.finish_turn("max_iterations", result, agent=self.name)
        return result

    def _build_result(self, summary: str) -> dict:
        return {
            "raw_output": summary,
            "exploit_result": self.exploit_result,
            "attempts_made": self.attempt_count,
            "type": "sandbox_verification_complete",
        }

    def _parse_final_response(self, content: str) -> dict:
        return self._build_result(content)
