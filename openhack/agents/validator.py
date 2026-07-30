"""
Validator agent for confirming vulnerabilities.
"""

import json
import logging
from typing import Optional

from .base import BaseAgent
from .llm import Message, ToolResult
from openhack.prompts import VALIDATOR_PROMPT, VALIDATOR_TOOL_INSTRUCTIONS, format_project_context

logger = logging.getLogger(__name__)


VALIDATE_FINDING_TOOL = {
    "name": "validate_finding",
    "description": "Report the validation result for the potential vulnerability.",
    "parameters": {
        "type": "object",
        "properties": {
            "finding_index": {"type": "integer", "description": "Index (1-based) of the finding"},
            "status": {"type": "string", "enum": ["confirmed", "false_positive", "needs_more_info"]},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "cvss_score": {"type": "number", "description": "CVSS 3.1 score (0.0 - 10.0)"},
            "evidence": {"type": "string", "description": "Evidence supporting the validation"},
            "poc": {"type": "string", "description": "Proof of concept"},
            "fix": {"type": "string", "description": "Recommended fix"}
        },
        "required": ["finding_index", "status", "confidence"]
    }
}

FINISH_VALIDATION_TOOL = {
    "name": "finish_validation",
    "description": "Call after validating all findings. Signals validation completion.",
    "parameters": {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "total_confirmed": {"type": "integer"},
            "total_false_positives": {"type": "integer"}
        },
        "required": ["summary", "total_confirmed", "total_false_positives"]
    }
}


class ValidatorAgent(BaseAgent):
    name = "validator"
    description = "Validating and confirming vulnerabilities"

    def __init__(self, *args, original_finding_index=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.validated_findings: list[dict] = []
        self.false_positives: list[dict] = []
        self.original_finding_index = original_finding_index

        if original_finding_index is not None:
            self.name = f"validator:finding_{original_finding_index}"
            self.description = f"Validating finding {original_finding_index}"

    def get_system_prompt(self, context: dict) -> str:
        findings = context.get("hunter", {}).get("findings", [])
        project_context = context.get("project_context", {})
        project_context_str = format_project_context(project_context)

        findings_text = ""
        for i, f in enumerate(findings, 1):
            findings_text += f"""
### Finding {i}
- **Category**: {f.get('category', 'Unknown')}
- **Severity**: {f.get('severity', 'Unknown')}
- **File**: {f.get('file_path', 'Unknown')}
- **Line**: {f.get('line_number', 'Unknown')}
- **Description**: {f.get('description', 'No description')}
- **Code**:
```
{f.get('code_snippet', 'No code snippet')}
```
"""

        base_prompt = VALIDATOR_PROMPT.format(
            findings=findings_text or "No findings to validate",
            project_context=project_context_str
        )
        base_prompt += VALIDATOR_TOOL_INSTRUCTIONS
        return base_prompt

    def get_tools(self) -> list[dict]:
        return super().get_tools() + [VALIDATE_FINDING_TOOL, FINISH_VALIDATION_TOOL]

    def _handle_validate_finding(self, args: dict) -> dict:
        status = args.get("status", "").lower()
        if self.original_finding_index is not None:
            original_index = self.original_finding_index
        else:
            original_index = args.get("finding_index", 1) - 1

        validation = {
            "original_index": original_index,
            "status": status,
            "confidence": args.get("confidence", "medium").lower(),
            "cvss_score": args.get("cvss_score"),
            "evidence": args.get("evidence", ""),
            "poc": args.get("poc"),
            "fix": args.get("fix"),
        }

        if status == "confirmed":
            self.validated_findings.append(validation)
        else:
            self.false_positives.append(validation)

        return {"status": "recorded", "finding_index": args.get("finding_index")}

    def _handle_finish_validation(self, args: dict) -> dict:
        return {
            "status": "validation_complete",
            "confirmed": len(self.validated_findings),
            "false_positives": len(self.false_positives),
        }

    async def run(self, task: str, context: Optional[dict] = None) -> dict:
        context = context or {}
        self.session.current_agent = self.name
        self.session.start_turn(task, self.name)
        self.validated_findings = []
        self.false_positives = []

        system_prompt = self.get_system_prompt(context)
        self.messages = []
        self._append_message(Message(role="user", content=task), source="run")
        self._seed_existing_instructions()

        max_iterations = 15 if self.original_finding_index is not None else 50
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
                self.session.add_trace(agent=self.name, event_type="thinking", content=response.content)

            if not response.tool_calls:
                self._append_message(
                    Message(
                        role="assistant",
                        content=response.content,
                        response_items=response.response_items,
                    ),
                    source="model_text_response",
                )
                result = self._build_result(response.content or "")
                self.session.finish_turn("completed", result, agent=self.name)
                return result

            assistant_msg = Message(
                role="assistant", content=response.content,
                response_items=response.response_items,
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

                if tool_call.name == "validate_finding":
                    result = self._handle_validate_finding(tool_call.arguments)
                elif tool_call.name == "finish_validation":
                    result = self._handle_finish_validation(tool_call.arguments)
                    should_finish = True
                else:
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
                tool_result = ToolResult(
                    tool_call_id=tool_call.id,
                    content=truncated_content,
                )
                self._append_message(
                    tool_result.to_message(), source=f"tool_result:{tool_call.name}"
                )

            if should_finish:
                result = self._build_result(response.content or "")
                self.session.finish_turn("completed", result, agent=self.name)
                return result

            if self.context_manager.needs_compaction():
                self.messages = self.context_manager.compact_messages(self.messages)
                logger.info(f"[{self.name}] Compacted message history ({self.context_manager.last_input_tokens} input tokens)")

        result = self._build_result("Max iterations reached")
        self.session.finish_turn("max_iterations", result, agent=self.name)
        return result

    def _build_result(self, summary: str) -> dict:
        return {
            "raw_output": summary,
            "validated_findings": self.validated_findings,
            "false_positives": self.false_positives,
            "type": "validation_complete",
        }

    def _parse_final_response(self, content: str) -> dict:
        return self._build_result(content)
