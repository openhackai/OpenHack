"""
Hunter agent for finding security vulnerabilities.
"""

import fnmatch
import json
import logging
from typing import Optional

import openai

from .base import BaseAgent
from .llm import Message, ToolResult
from openhack.config import settings
from openhack.prompts import (
    HUNTER_PROMPT,
    ALL_FRAMEWORK_PROMPTS,
    HUNTER_TOOL_INSTRUCTIONS,
    HUNTER_CONTINUATION_NO_FINDINGS,
    HUNTER_CONTINUATION_LOOP,
    HUNTER_CONTINUATION_NO_PROGRESS,
    format_project_context,
)
from openhack.categories import CATEGORIES, normalize_category

logger = logging.getLogger(__name__)


REPORT_FINDING_TOOL = {
    "name": "report_finding",
    "description": "Report a potential security vulnerability found during analysis. You MUST call this tool for EACH vulnerability you discover.",
    "parameters": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": CATEGORIES,
                "description": "Category of the vulnerability. MUST be one of the allowed values."
            },
            "severity": {
                "type": "string",
                "enum": ["critical", "high", "medium", "low", "info"],
                "description": "Severity level"
            },
            "file_path": {"type": "string", "description": "Path to the vulnerable file"},
            "line_number": {"type": "integer", "description": "Line number"},
            "description": {"type": "string", "description": "Detailed description of the vulnerability"},
            "code_snippet": {"type": "string", "description": "The vulnerable code snippet"},
            "confidence": {
                "type": "string",
                "enum": ["high", "medium", "low"],
                "description": "Confidence level"
            }
        },
        "required": ["category", "severity", "file_path", "description"]
    }
}

FINISH_HUNT_TOOL = {
    "name": "finish_hunt",
    "description": "Call this tool ONLY after you have reported ALL vulnerabilities. Signals hunt completion.",
    "parameters": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "Brief summary of findings"},
            "total_findings": {"type": "integer", "description": "Total vulnerabilities found"},
            "critical_count": {"type": "integer", "description": "Critical findings count"},
            "high_count": {"type": "integer", "description": "High findings count"}
        },
        "required": ["summary", "total_findings"]
    }
}


class HunterAgent(BaseAgent):
    name = "hunter"
    description = "Vulnerability hunting"
    max_iterations: int = 50

    DEFAULT_CATEGORIES = [
        "idor", "xss", "csrf", "ssrf", "injection",
        "auth_bypass", "data_exposure", "middleware_bypass",
        "server_actions", "misconfiguration",
    ]

    def __init__(self, *args, vuln_categories=None, group_name=None, framework=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.vuln_categories = vuln_categories or self.DEFAULT_CATEGORIES
        self.group_name = group_name
        self.framework = framework
        self.findings: list[dict] = []
        self._files_read: set[str] = set()

        if group_name:
            self.name = f"hunter:{group_name}"
            self.description = f"Vulnerability hunting ({group_name})"

    def get_system_prompt(self, context: dict) -> str:
        recon_context = context.get("recon", {}).get("summary", "No recon data available")
        project_context = context.get("project_context", {})
        project_context_str = format_project_context(project_context)

        category_prompts = []
        if self.framework is not None:
            framework_prompts = ALL_FRAMEWORK_PROMPTS.get(self.framework, {})
            for category in self.vuln_categories:
                if category in framework_prompts:
                    category_prompts.append(framework_prompts[category])

        full_prompt = HUNTER_PROMPT.format(
            recon_context=recon_context,
            project_context=project_context_str
        )
        if category_prompts:
            full_prompt += "\n\n## Detailed Vulnerability Guidance\n\n"
            full_prompt += "\n\n---\n\n".join(category_prompts)

        full_prompt += HUNTER_TOOL_INSTRUCTIONS
        return full_prompt

    def get_tools(self) -> list[dict]:
        base_tools = super().get_tools()
        return base_tools + [REPORT_FINDING_TOOL, FINISH_HUNT_TOOL]

    @staticmethod
    def _is_excluded_path(file_path: str) -> bool:
        """Check if a file path matches any scan exclude pattern."""
        for pattern in settings.scan_exclude_patterns:
            if fnmatch.fnmatch(file_path, pattern):
                return True
            # Also check without leading ./ or /
            normalized = file_path.lstrip("./")
            if fnmatch.fnmatch(normalized, pattern):
                return True
            # Check if any path segment matches (for patterns like "**/test/**")
            if "**" in pattern:
                # Convert glob to work with fnmatch on full paths
                simple_pattern = pattern.replace("**/", "*/")
                if fnmatch.fnmatch(file_path, simple_pattern) or fnmatch.fnmatch(normalized, simple_pattern):
                    return True
                # Direct substring check for directory patterns
                dir_part = pattern.replace("**/", "").replace("/**", "").replace("*", "")
                if dir_part and f"/{dir_part}/" in f"/{normalized}":
                    return True
        return False

    def _correct_line_number(self, file_path: str, line_number: int, code_snippet: str) -> int:
        """Correct the reported line number by searching for the code snippet in the file.

        Models often get the code snippet right but report the wrong line number
        due to context window drift. This does a simple search to fix it.
        """
        if not file_path or not code_snippet:
            return line_number

        try:
            result = self.tools.execute_tool("read_file", {"path": file_path})
        except Exception:
            return line_number

        content = result.get("content", "") if isinstance(result, dict) else str(result)
        if not content:
            return line_number

        lines = content.split("\n")
        # Strip line-number prefix if present (from read_file format: "123\tcontent")
        clean_lines = []
        for line in lines:
            if "\t" in line:
                clean_lines.append(line.split("\t", 1)[1])
            else:
                clean_lines.append(line)

        # Clean up the snippet for matching — take the most distinctive line
        snippet_lines = [s.strip() for s in code_snippet.strip().split("\n") if s.strip()]
        if not snippet_lines:
            return line_number

        # Try to find the first non-trivial line of the snippet in the file
        search_line = None
        for sl in snippet_lines:
            # Skip trivial lines (just braces, empty, common keywords)
            if sl in ("{", "}", "});", ");", "*/", "/*", "//"):
                continue
            if len(sl) > 10:  # Needs to be distinctive enough
                search_line = sl
                break
        if not search_line:
            search_line = snippet_lines[0]

        # Search for the line in the file
        for i, file_line in enumerate(clean_lines):
            if search_line in file_line.strip():
                corrected = i + 1  # 1-indexed
                if corrected != line_number:
                    logger.debug(f"Corrected line number {line_number} → {corrected} for {file_path}")
                return corrected

        # Snippet not found — return original
        return line_number

    def _handle_report_finding(self, args: dict) -> dict:
        file_path = args.get("file_path", "")

        # Pre-filter: reject findings in excluded paths
        if file_path and self._is_excluded_path(file_path):
            logger.info(f"Finding rejected (excluded path): {args.get('category', '?')} in {file_path}")
            return {"status": "rejected", "reason": f"File is in an excluded path (test/CLI/docs/examples): {file_path}"}

        # Correct line number by searching for the code snippet in the file
        line_number = args.get("line_number")
        code_snippet = args.get("code_snippet")
        if line_number and code_snippet and file_path:
            line_number = self._correct_line_number(file_path, line_number, code_snippet)

        finding = {
            "category": normalize_category(args.get("category", "unknown")),
            "severity": args.get("severity", "medium").lower(),
            "file_path": file_path,
            "line_number": line_number,
            "description": args.get("description", ""),
            "code_snippet": code_snippet,
            "confidence": args.get("confidence", "medium").lower(),
            "validated": False,
        }
        self.findings.append(finding)
        logger.info(f"Finding reported: {finding['category']} in {finding['file_path']}")
        return {"status": "recorded", "finding_id": len(self.findings)}

    def _handle_finish_hunt(self, args: dict) -> dict:
        logger.info(f"Hunt finished: {len(self.findings)} findings")
        return {"status": "hunt_complete", "findings_reported": len(self.findings)}

    async def run(self, task: str, context: Optional[dict] = None) -> dict:
        context = context or {}
        self.session.current_agent = self.name
        self.session.start_turn(task, self.name)
        self.findings = []
        self._files_read = set()

        system_prompt = self.get_system_prompt(context)
        self.messages = []
        self._append_message(Message(role="user", content=task), source="run")
        self._seed_existing_instructions()

        max_iterations = self.max_iterations
        iteration = 0
        recent_tools: list[str] = []
        iterations_since_finding = 0
        consecutive_reads = 0
        analysis_checkpoints_sent = 0
        analysis_mode = False
        analysis_mode_turns = 0
        continuation_prompts_sent = 0
        max_continuation_prompts = 3
        LOOP_DETECTION_THRESHOLD = 6
        NO_PROGRESS_THRESHOLD = 15
        ANALYSIS_CHECKPOINT_INTERVAL = 5

        while iteration < max_iterations:
            if self.session.cancelled:
                break
            await self.session.wait_if_paused()
            if self.session.cancelled:
                break
            iteration += 1
            iterations_since_finding += 1

            self._inject_pending_instructions()

            if analysis_mode:
                tools = [REPORT_FINDING_TOOL, FINISH_HUNT_TOOL]
                forced_tool_choice = "required"
            else:
                tools = self.get_tools()
                forced_tool_choice = None

            self._preflight_compact(system_prompt)

            try:
                response = await self.llm.chat(
                    messages=self.messages,
                    tools=tools,
                    system=system_prompt,
                    tool_choice=forced_tool_choice,
                )
            except openai.BadRequestError as e:
                err_msg = str(e)
                if "too long" in err_msg or "too many tokens" in err_msg.lower() or "context length" in err_msg:
                    logger.warning(f"[{self.name}] Context overflow — compacting and retrying")
                    self.messages = self.context_manager.compact_messages(self.messages, keep_recent_turns=2)
                    estimated = self._estimate_tokens(self.messages, system_prompt)
                    if estimated > self.context_manager.context_window_limit * 0.85:
                        self.messages = self.context_manager.emergency_compact(self.messages)
                    try:
                        response = await self.llm.chat(
                            messages=self.messages,
                            tools=tools,
                            system=system_prompt,
                            tool_choice=forced_tool_choice,
                        )
                    except openai.BadRequestError as e2:
                        err_msg2 = str(e2)
                        if "too long" in err_msg2 or "context length" in err_msg2:
                            logger.warning(f"[{self.name}] Still overflowing — emergency compaction")
                            self.messages = self.context_manager.emergency_compact(self.messages)
                            response = await self.llm.chat(
                                messages=self.messages,
                                tools=tools,
                                system=system_prompt,
                                tool_choice=forced_tool_choice,
                            )
                        else:
                            raise
                else:
                    raise

            self._record_response_usage(response)

            if response.content:
                self.session.add_trace(agent=self.name, event_type="thinking", content=response.content)

            if not response.tool_calls:
                if analysis_mode:
                    analysis_mode_turns += 1
                    if analysis_mode_turns >= 3:
                        analysis_mode = False
                        analysis_mode_turns = 0
                        continue
                    self._append_message(Message(
                        role="user",
                        content=(
                            "You responded with text but did not call any tools. "
                            "You MUST call report_finding or finish_hunt. "
                            "Do NOT respond with text — use the tools."
                        ),
                    ), source="analysis_mode_guard")
                    continue
                if len(self.findings) == 0 and continuation_prompts_sent < max_continuation_prompts:
                    continuation_prompts_sent += 1
                    self._append_message(
                        Message(role="user", content=HUNTER_CONTINUATION_NO_FINDINGS),
                        source="no_findings_guard",
                    )
                    continue
                self._append_message(
                    Message(role="assistant", content=response.content),
                    source="model_text_response",
                )
                result = self._build_result(response.content or "")
                self.session.finish_turn("completed", result, agent=self.name)
                return result

            current_tools = [tc.name for tc in response.tool_calls]
            recent_tools.extend(current_tools)
            recent_tools = recent_tools[-10:]

            if len(recent_tools) >= LOOP_DETECTION_THRESHOLD:
                last_n = recent_tools[-LOOP_DETECTION_THRESHOLD:]
                if len(set(last_n)) == 1 and last_n[0] in ("list_dir", "glob"):
                    if continuation_prompts_sent < max_continuation_prompts:
                        continuation_prompts_sent += 1
                        self._append_message(
                            Message(
                                role="user",
                                content=HUNTER_CONTINUATION_LOOP.format(
                                    tool_name=last_n[0]
                                ),
                            ),
                            source="loop_guard",
                        )
                        recent_tools = []
                        continue

            assistant_msg = Message(
                role="assistant",
                content=response.content,
                tool_calls=[
                    {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)}}
                    for tc in response.tool_calls
                ],
            )
            self._append_message(assistant_msg, source="model_tool_response")

            should_finish = False
            has_only_reads = all(tc.name == "read_file" for tc in response.tool_calls)
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

                if tool_call.name == "report_finding":
                    result = self._handle_report_finding(tool_call.arguments)
                    iterations_since_finding = 0
                    consecutive_reads = 0
                    analysis_mode = False
                elif tool_call.name == "finish_hunt":
                    result = self._handle_finish_hunt(tool_call.arguments)
                    should_finish = True
                else:
                    try:
                        result = self.tools.execute_tool(tool_call.name, tool_call.arguments)
                    except Exception as e:
                        result = {"error": f"Tool execution failed: {e}"}
                        logger.warning(f"[{self.name}] Tool {tool_call.name} failed: {e}")
                    if tool_call.name == "read_file":
                        file_path = tool_call.arguments.get("path", "")
                        if file_path:
                            self._files_read.add(file_path)

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

            if has_only_reads:
                consecutive_reads += 1

            if analysis_mode and response.tool_calls:
                has_action = any(tc.name in ("report_finding", "finish_hunt") for tc in response.tool_calls)
                if has_action:
                    analysis_mode = False
                    analysis_mode_turns = 0

            if (consecutive_reads >= ANALYSIS_CHECKPOINT_INTERVAL
                    and len(self.findings) == 0
                    and analysis_checkpoints_sent < 5):
                analysis_checkpoints_sent += 1
                analysis_mode = True
                analysis_mode_turns = 0
                files_list = ", ".join(sorted(self._files_read)[-10:])
                self._append_message(Message(
                    role="user",
                    content=(
                        f"[ANALYSIS CHECKPOINT] You have read {len(self._files_read)} files without reporting "
                        f"any findings. STOP reading new files and call report_finding NOW.\n\n"
                        f"Files read: {files_list}\n\n"
                        f"You MUST call report_finding (not just describe in text) for EACH of these checks:\n"
                        f"1. IDOR: Does any endpoint load an object by ID without verifying the requesting "
                        f"user owns it? (e.g., GET /api/thing/<id> checks auth but not ownership)\n"
                        f"2. ORM escape hatches: Any .raw(), .extra(), RawSQL, cursor.execute() with "
                        f"string formatting instead of parameterized queries?\n"
                        f"3. Mass assignment: Can request.data set privileged fields (is_staff, role, "
                        f"organization) via serializer without explicit field restrictions?\n"
                        f"4. SSRF: Any user-controlled URL passed to requests/httpx/urllib without validation?\n"
                        f"5. Auth bypass: Any endpoint missing permission_classes or using weaker auth "
                        f"than sibling endpoints?\n\n"
                        f"Report at confidence='medium' if uncertain. Under-reporting is a failure mode. "
                        f"You MUST use the report_finding tool, not describe findings in text."
                    ),
                ), source="analysis_checkpoint")
                consecutive_reads = 0
                logger.info(f"[{self.name}] Analysis checkpoint at iteration {iteration}, {len(self._files_read)} files read")

            if self.context_manager.needs_compaction():
                self.messages = self.context_manager.compact_messages(self.messages)
                logger.info(f"[{self.name}] Compacted message history ({self.context_manager.last_input_tokens} input tokens)")

            if (iterations_since_finding >= NO_PROGRESS_THRESHOLD and
                len(self.findings) == 0 and
                continuation_prompts_sent < max_continuation_prompts):
                continuation_prompts_sent += 1
                self._append_message(Message(
                    role="user",
                    content=HUNTER_CONTINUATION_NO_PROGRESS.format(
                        files_count=len(self._files_read), iteration=iteration,
                    ),
                ), source="no_progress_guard")
                iterations_since_finding = 0

        result = self._build_result("Max iterations reached")
        self.session.finish_turn(
            "cancelled" if self.session.cancelled else "max_iterations",
            result,
            agent=self.name,
        )
        return result

    def _build_result(self, summary: str) -> dict:
        return {
            "raw_output": summary,
            "findings": self.findings,
            "type": "hunt_complete",
            "files_analyzed": sorted(self._files_read),
        }

    def _parse_final_response(self, content: str) -> dict:
        return self._build_result(content)
