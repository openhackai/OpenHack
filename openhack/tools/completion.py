"""Explicit completion contract for the interactive agent."""

from typing import Any


class CompletionTools:
    """Optional explicit completion signal for tool-driven agent turns."""

    def get_tool_definitions(self) -> list[dict]:
        return [
            {
                "name": "finish_task",
                "description": (
                    "Finish the current task only after all promised actions and "
                    "verification are complete. Put the complete user-facing answer "
                    "in summary. Do not call this merely because you have written a "
                    "plan or are about to perform another action."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "summary": {
                            "type": "string",
                            "description": (
                                "Complete final answer for the operator, not a recap "
                                "of what you did. If you already gave the complete "
                                "answer as prose, copy that prose here verbatim."
                            ),
                        },
                        "reason": {
                            "type": "string",
                            "enum": [
                                "completed",
                                "needs_user_input",
                                "blocked",
                                "no_action_needed",
                            ],
                            "description": "Why control is being returned.",
                        },
                        "verification": {
                            "type": "string",
                            "description": "What was checked to support completion.",
                        },
                    },
                    "required": ["summary", "reason"],
                },
            }
        ]

    def execute_tool(self, name: str, arguments: dict) -> Any:
        if name != "finish_task":
            return {"error": f"Unknown tool: {name}"}
        args = arguments or {}
        summary = str(args.get("summary") or "").strip()
        reason = str(args.get("reason") or "").strip()
        if not summary:
            return {"error": "finish_task requires a non-empty summary"}
        if reason not in {
            "completed",
            "needs_user_input",
            "blocked",
            "no_action_needed",
        }:
            return {"error": "finish_task has an invalid reason"}
        return {
            "finished": True,
            "summary": summary,
            "reason": reason,
            "verification": str(args.get("verification") or "").strip(),
        }
