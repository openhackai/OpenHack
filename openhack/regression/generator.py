"""
Regression test generator agent.

Reads the repository (read-only, jailed to the vulnerable revision's
workspace copy) and submits one new test file through
``submit_regression_test``. The pipeline validates every submission and, when
one fails, sends precise feedback back into the same conversation so the
agent can correct it rather than starting over.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from openhack.agents.base import BaseAgent
from openhack.agents.llm import Message, ToolResult
from openhack.prompts import (
    REGRESSION_TEST_PROMPT,
    REGRESSION_TEST_TOOL_INSTRUCTIONS,
    format_project_context,
)
from .evidence import scrub_text

if TYPE_CHECKING:
    from openhack.agents.session import Finding
    from .frameworks import FrameworkAdapter, FrameworkProfile
    from .invariants import VulnerabilityClassSpec
    from .models import SecurityInvariant

logger = logging.getLogger(__name__)


SUBMIT_REGRESSION_TEST_TOOL = {
    "name": "submit_regression_test",
    "description": (
        "Submit the complete security regression test file. OpenHack validates it "
        "in an isolated sandbox against the vulnerable and the remediated code."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "New test file path relative to the project root (must not exist yet)",
            },
            "code": {"type": "string", "description": "Complete contents of the test file"},
            "invariant_tests": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Test function names asserting the invariant (fail on vulnerable code)",
            },
            "control_tests": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Test function names proving legitimate access works (pass on both)",
            },
            "summary": {"type": "string", "description": "One sentence: what the test protects"},
        },
        "required": ["path", "code", "invariant_tests", "control_tests"],
    },
}


@dataclass
class CandidateTest:
    path: str
    code: str
    invariant_tests: list[str] = field(default_factory=list)
    control_tests: list[str] = field(default_factory=list)
    summary: str = ""


def _string_list(value) -> Optional[list[str]]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        return None
    return [v.strip().split("::")[-1].split("[", 1)[0] for v in value]


class RegressionTestGeneratorAgent(BaseAgent):
    """Writes a regression test for one verified finding."""

    name = "regression_test"
    description = "Generating security regression test"

    def __init__(
        self,
        *args,
        finding: "Finding",
        invariant: "SecurityInvariant",
        spec: "VulnerabilityClassSpec",
        adapter: "FrameworkAdapter",
        profile: "FrameworkProfile",
        suggested_path: str,
        finding_index: int = 0,
        max_iterations: int = 24,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.finding = finding
        self.invariant = invariant
        self.spec = spec
        self.adapter = adapter
        self.profile = profile
        self.suggested_path = suggested_path
        self.max_iterations = max_iterations
        self.submission: Optional[CandidateTest] = None
        self.name = f"regression_test:finding_{finding_index}"
        self.description = f"Generating regression test for finding {finding_index}"
        self._system_prompt = self.get_system_prompt({})

    def get_system_prompt(self, context: dict) -> str:
        f = self.finding
        evidence = f.exploit_evidence or {}
        request = evidence.get("exploit_request") or {}
        request_line = " ".join(
            part for part in (request.get("method"), request.get("path")) if part
        ) or "not recorded"
        location = f.file_path + (f":{f.line_number}" if f.line_number else "")
        finding_details = f"""### {f.title}
- **Category**: {f.category} · **Severity**: {f.severity}
- **Location**: {location or 'unknown'}
- **Description**: {scrub_text(f.description or 'No description')}
- **Vulnerable code**:
```
{f.code_snippet or 'No code snippet'}
```
- **Exploited request (sandbox)**: `{request_line}`
- **Exploit evidence (redacted)**: {scrub_text(evidence.get('evidence') or 'none recorded')}
- **Recommended remediation** (context only — do not assert implementation details of any particular fix):
{scrub_text(f.fix or 'none recorded')}
"""
        inv = self.invariant
        invariant_details = f"""- **Invariant ({inv.cwe})**: {inv.statement}
- **Endpoint**: `{inv.method} {inv.path_template}` (object identifier: `{inv.object_param or 'unknown'}`, in {inv.param_location or 'unknown location'})
- **Must hold**: {inv.denial}
- **Must keep working**: {inv.control}
"""
        prompt = REGRESSION_TEST_PROMPT.format(
            project_context=format_project_context(self.session.project_context),
            finding_details=finding_details,
            invariant_details=invariant_details,
            framework_details=self.adapter.prompt_context(self.profile),
            suggested_path=self.suggested_path,
            class_guidance=self.spec.generation_guidance(inv),
        )
        return prompt + REGRESSION_TEST_TOOL_INSTRUCTIONS

    def get_tools(self) -> list[dict]:
        return super().get_tools() + [SUBMIT_REGRESSION_TEST_TOOL]

    async def generate(self, note: str = "") -> Optional[CandidateTest]:
        """Start the conversation and return the first submission (or None)."""
        task = (
            "Write a security regression test for the confirmed vulnerability above, "
            "using the repository's existing test framework, then submit it."
        )
        if note:
            task += "\n\n" + note
        self.session.current_agent = self.name
        self.session.start_turn(task, self.name)
        self.messages = []
        self._append_message(Message(role="user", content=task), source="run")
        self._seed_existing_instructions()
        self.session.record_event(
            "agent_configuration",
            {
                "agent": self.name,
                "model": self.llm.model,
                "provider": getattr(self.llm, "provider", None),
                "system_prompt": self._system_prompt,
                "tools": self.get_tools(),
            },
            agent=self.name,
        )
        submission = await self._loop()
        self.session.finish_turn(
            "completed" if submission else "no_submission",
            {"submitted": submission is not None}, agent=self.name,
        )
        return submission

    async def revise(self, feedback: str) -> Optional[CandidateTest]:
        """Send validation feedback and return the corrected submission (or None)."""
        self.session.start_turn("revise regression test", self.name)
        self._append_message(
            Message(
                role="user",
                content=(
                    "[VALIDATION FEEDBACK] OpenHack rejected the submitted test:\n"
                    f"{feedback}\n\nFix the problem and submit the complete corrected file "
                    "with submit_regression_test."
                ),
            ),
            source="validation_feedback",
        )
        submission = await self._loop()
        self.session.finish_turn(
            "completed" if submission else "no_submission",
            {"submitted": submission is not None}, agent=self.name,
        )
        return submission

    def _handle_submit(self, args: dict) -> dict:
        path = args.get("path")
        code = args.get("code")
        invariant_tests = _string_list(args.get("invariant_tests"))
        control_tests = _string_list(args.get("control_tests", []))
        problems = []
        if not isinstance(path, str) or not path.strip():
            problems.append("`path` must be a non-empty string")
        if not isinstance(code, str) or not code.strip():
            problems.append("`code` must contain the complete test file")
        if not invariant_tests:
            problems.append("`invariant_tests` must list at least one test function name")
        if control_tests is None:
            problems.append("`control_tests` must be a list of test function names")
        if problems or not isinstance(path, str) or not isinstance(code, str) or not invariant_tests:
            return {"error": "Submission rejected: " + "; ".join(problems)}
        self.submission = CandidateTest(
            path=path.strip(),
            code=code,
            invariant_tests=invariant_tests,
            control_tests=control_tests or [],
            summary=str(args.get("summary") or "").strip(),
        )
        return {
            "status": "received",
            "note": "OpenHack will now validate the test in the sandbox and report back.",
        }

    async def _loop(self) -> Optional[CandidateTest]:
        self.submission = None
        nudged = False
        for _ in range(self.max_iterations):
            if self.session.cancelled:
                return None
            await self.session.wait_if_paused()
            self._inject_pending_instructions()
            self._preflight_compact(self._system_prompt)

            response = await self.llm.chat(
                messages=self.messages, tools=self.get_tools(), system=self._system_prompt,
            )
            self._record_response_usage(response)
            if response.content:
                self.session.add_trace(
                    agent=self.name, event_type="thinking", content=response.content,
                    model_call_id=response.model_call_id,
                )

            if not response.tool_calls:
                self._append_message(
                    Message(role="assistant", content=response.content,
                            response_items=response.response_items),
                    source="model_text_response",
                )
                if nudged:
                    return None
                nudged = True
                self._append_message(
                    Message(role="user", content=(
                        "You have not submitted a test. Call submit_regression_test "
                        "with the complete test file now."
                    )),
                    source="submission_guard",
                )
                continue

            self._append_message(
                Message(
                    role="assistant", content=response.content,
                    response_items=response.response_items,
                    tool_calls=[
                        {"id": tc.id, "type": "function",
                         "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)}}
                        for tc in response.tool_calls
                    ],
                ),
                source="model_tool_response",
            )
            for tool_call in response.tool_calls:
                self.session.add_trace(
                    agent=self.name, event_type="tool_call",
                    content=f"Calling {tool_call.name}",
                    tool_name=tool_call.name, tool_input=tool_call.arguments,
                    model_call_id=response.model_call_id, tool_call_id=tool_call.id,
                )
                try:
                    if tool_call.name == "submit_regression_test":
                        result = self._handle_submit(tool_call.arguments)
                    elif self.tools.is_async_tool(tool_call.name):
                        result = await self.tools.execute_tool_async(
                            tool_call.name, tool_call.arguments
                        )
                    else:
                        result = await asyncio.to_thread(
                            self.tools.execute_tool, tool_call.name, tool_call.arguments
                        )
                except Exception as exc:
                    result = {"error": f"{tool_call.name} failed: {exc}"}
                self.session.add_trace(
                    agent=self.name, event_type="tool_result",
                    content=f"Result from {tool_call.name}",
                    tool_name=tool_call.name, tool_output=result,
                    model_call_id=response.model_call_id, tool_call_id=tool_call.id,
                )
                raw = json.dumps(result, default=str) if isinstance(result, dict) else str(result)
                truncated = self.context_manager.truncate_tool_result(tool_call.name, raw)
                self._append_message(
                    ToolResult(tool_call_id=tool_call.id, content=truncated).to_message(),
                    source=f"tool_result:{tool_call.name}",
                )
            if self.submission is not None:
                return self.submission
            if self.context_manager.needs_compaction():
                self.messages = self.context_manager.compact_messages(self.messages)
        logger.info(f"[{self.name}] no submission within {self.max_iterations} iterations")
        return None
