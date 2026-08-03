"""
Context window management for long-running agents.

Provides proactive tool result truncation and reactive message compaction
to prevent agents from exceeding model context limits.
"""

import json
import logging
from typing import Optional

from .llm import Message

logger = logging.getLogger(__name__)


MODEL_CONTEXT_LIMITS: dict[str, int] = {
    "grok-4.5": 500_000,
    "kimi-k2.5": 128_000,
    "glm-5.2": 128_000,
    "gemma-4-31b": 128_000,
    "mistral-large-2512": 128_000,
    "gpt-5.6-luna": 1_050_000,
    "gpt-5.6-luna-pro": 1_050_000,
    "gpt-5.6-terra": 1_050_000,
    "gpt-5.6-terra-pro": 1_050_000,
    "gpt-5.6-sol": 1_050_000,
    "gpt-5.6-sol-pro": 1_050_000,
}

DEFAULT_CONTEXT_LIMIT = 128_000


class ContextWindowManager:
    """Manages context window usage for an agent via truncation and compaction."""

    def __init__(
        self,
        context_window_limit: int = DEFAULT_CONTEXT_LIMIT,
        compaction_threshold: float = 0.70,
        tool_result_max_lines: int = 200,
    ):
        self.context_window_limit = context_window_limit
        self.compaction_threshold = compaction_threshold
        self.tool_result_max_lines = tool_result_max_lines
        self.last_input_tokens: int = 0

    def update_usage(self, input_tokens: int) -> None:
        """Update with the latest input token count from an LLM response."""
        self.last_input_tokens = input_tokens

    def needs_compaction(self) -> bool:
        """Check if context usage has exceeded the compaction threshold."""
        return self.last_input_tokens > self.context_window_limit * self.compaction_threshold

    # ── Proactive truncation (before insertion) ─────────────────────────

    def truncate_tool_result(self, tool_name: str, content: str) -> str:
        """Truncate a tool result before inserting it into the message history.

        Tool results are JSON-serialized dicts (via json.dumps), so we parse
        the JSON, truncate the relevant inner field, and re-serialize.
        """
        try:
            data = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            if len(content) > 10_000:
                return content[:8_000] + f"\n\n[... truncated, {len(content)} total chars ...]"
            return content

        if not isinstance(data, dict) or "error" in data:
            return content

        if tool_name == "read_file":
            return self._truncate_read_file(data)
        if tool_name == "grep":
            return self._truncate_grep(data)
        if tool_name == "list_dir":
            return self._truncate_list_dir(data)
        if tool_name == "glob":
            return self._truncate_glob_result(data)

        return self._truncate_generic(data, content)

    def _truncate_generic(self, data: dict, content: str) -> str:
        """Shrink an oversized tool result by trimming its longest text fields.

        The previous approach sliced the serialized JSON at 8k, which handed the
        model a syntactically broken object with its tail lopped off mid-value.
        Trimming the fields instead keeps the result valid and readable, so the
        model still sees every key — just with long bodies shortened. Big web
        results (page text, per-result content) are the main customers.
        """
        if len(content) <= 10_000:
            return content
        budget = 8_000
        overflow = len(content) - budget

        def _fields(node, path=()):
            """Yield (container, key, text) for every long string, recursively."""
            if isinstance(node, dict):
                for k, v in node.items():
                    if isinstance(v, str):
                        yield node, k, v
                    else:
                        yield from _fields(v, path + (k,))
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    if isinstance(v, str):
                        yield node, i, v
                    else:
                        yield from _fields(v, path + (i,))

        # Trim longest-first so one huge body is cut before many short ones.
        targets = sorted(_fields(data), key=lambda t: -len(t[2]))
        for container, key, text in targets:
            if overflow <= 0:
                break
            if len(text) <= 200:
                continue
            keep = max(200, len(text) - overflow)
            if keep >= len(text):
                continue
            container[key] = text[:keep] + f"… [truncated {len(text) - keep} chars]"
            overflow -= (len(text) - keep)
        try:
            out = json.dumps(data)
        except (TypeError, ValueError):
            return content[:budget] + f"\n\n[... truncated, {len(content)} total chars ...]"
        # Still oversized (e.g. thousands of short fields) — fall back to a hard
        # cut, which at least bounds the context.
        if len(out) > 10_000:
            return out[:budget] + f"\n\n[... truncated, {len(out)} total chars ...]"
        return out

    def _truncate_read_file(self, data: dict) -> str:
        """Truncate read_file by trimming the content field's lines."""
        file_content = data.get("content", "")
        lines = file_content.split("\n")
        max_lines = self.tool_result_max_lines

        if len(lines) <= max_lines:
            return json.dumps(data)

        head = lines[:100]
        tail = lines[-50:]
        omitted = len(lines) - 150
        data["content"] = "\n".join(head) + f"\n\n[... {omitted} lines omitted ...]\n\n" + "\n".join(tail)
        data["truncated"] = True
        return json.dumps(data)

    def _truncate_grep(self, data: dict) -> str:
        """Truncate grep by trimming the matches list."""
        matches = data.get("matches", [])
        if len(matches) <= 50:
            return json.dumps(data)

        head = matches[:30]
        tail = matches[-10:]
        omitted = len(matches) - 40
        data["matches"] = head + [{"note": f"... {omitted} matches omitted ..."}] + tail
        data["truncated"] = True
        return json.dumps(data)

    def _truncate_list_dir(self, data: dict) -> str:
        """Truncate list_dir by trimming the entries list."""
        entries = data.get("entries", [])
        if len(entries) <= 100:
            return json.dumps(data)

        head = entries[:50]
        tail = entries[-20:]
        omitted = len(entries) - 70
        data["entries"] = head + [{"note": f"... {omitted} entries omitted ..."}] + tail
        data["truncated"] = True
        return json.dumps(data)

    def _truncate_glob_result(self, data: dict) -> str:
        """Truncate glob by trimming the matches list."""
        matches = data.get("matches", [])
        if len(matches) <= 100:
            return json.dumps(data)

        head = matches[:50]
        tail = matches[-20:]
        omitted = len(matches) - 70
        data["matches"] = head + [f"... {omitted} matches omitted ..."] + tail
        data["truncated"] = True
        return json.dumps(data)

    # ── Reactive compaction (on threshold breach) ───────────────────────

    def compact_messages(self, messages: list[Message], keep_recent_turns: int = 3) -> list[Message]:
        """Compact older messages by summarizing tool results.

        Preserves:
        - The first message (original task)
        - All [USER INSTRUCTION] messages
        - The last ``keep_recent_turns`` full turns (assistant + tool results)
        Never removes messages — only replaces content to keep tool_call/result pairing.
        """
        if len(messages) <= 4:
            return messages

        # Find turn boundaries: each turn starts with an assistant message that has tool_calls
        turn_starts: list[int] = []
        for i, msg in enumerate(messages):
            if msg.role == "assistant" and msg.tool_calls:
                turn_starts.append(i)

        if len(turn_starts) <= keep_recent_turns:
            return messages

        # Messages from the start of the Nth-from-last turn onward are protected
        protect_from = turn_starts[-keep_recent_turns]

        compacted = []
        for i, msg in enumerate(messages):
            if i == 0:
                # Always keep the original task intact
                compacted.append(msg)
            elif i >= protect_from:
                # Recent turns — keep intact
                compacted.append(msg)
            elif msg.role == "user" and msg.content and "[USER INSTRUCTION]" in msg.content:
                # Always keep user instructions
                compacted.append(msg)
            elif msg.role == "tool":
                # Older tool result — summarize
                tool_name = self._infer_tool_name(messages, i)
                summary = self._summarize_tool_result(tool_name, msg.content or "")
                compacted.append(Message(
                    role=msg.role,
                    content=summary,
                    tool_call_id=msg.tool_call_id,
                    name=msg.name,
                ))
            elif msg.role == "assistant" and msg.content and len(msg.content) > 200:
                # Older assistant thinking — truncate but keep tool_calls structure
                compacted.append(Message(
                    role=msg.role,
                    content=msg.content[:200] + "...",
                    tool_calls=msg.tool_calls,
                    reasoning_content=None,
                ))
            else:
                compacted.append(msg)

        # Reset token counter so we don't re-compact before the next LLM call
        # updates it with the actual (lower) token count.
        self.last_input_tokens = 0

        logger.info(
            f"Compacted messages: {len(messages)} msgs, "
            f"protected last {keep_recent_turns} turns from idx {protect_from}"
        )
        return compacted

    def emergency_compact(self, messages: list[Message]) -> list[Message]:
        """Aggressive compaction for when normal compaction isn't enough.

        Keeps only the first message, user instructions, and the last 2 turns.
        All older tool results are replaced with one-line summaries.
        All older assistant messages are truncated to 100 chars.
        Protected-turn tool results are also truncated to prevent overflow.
        """
        if len(messages) <= 3:
            return messages

        turn_starts: list[int] = []
        for i, msg in enumerate(messages):
            if msg.role == "assistant" and msg.tool_calls:
                turn_starts.append(i)

        protect_from = turn_starts[-2] if len(turn_starts) >= 2 else turn_starts[-1] if turn_starts else len(messages)

        compacted = []
        for i, msg in enumerate(messages):
            if i == 0:
                content = msg.content or ""
                if len(content) > 2000:
                    compacted.append(Message(role=msg.role, content=content[:2000] + "\n[... truncated ...]"))
                else:
                    compacted.append(msg)
            elif i >= protect_from:
                if msg.role == "tool" and msg.content and len(msg.content) > 4000:
                    tool_name = self._infer_tool_name(messages, i)
                    truncated = msg.content[:3000] + f"\n\n[... {tool_name} result truncated from {len(msg.content)} chars for context management ...]"
                    compacted.append(Message(
                        role=msg.role,
                        content=truncated,
                        tool_call_id=msg.tool_call_id,
                        name=msg.name,
                    ))
                elif msg.role == "assistant" and msg.content and len(msg.content) > 500:
                    compacted.append(Message(
                        role=msg.role,
                        content=msg.content[:500] + "...",
                        tool_calls=msg.tool_calls,
                        reasoning_content=None,
                    ))
                else:
                    compacted.append(msg)
            elif msg.role == "user" and msg.content and "[USER INSTRUCTION]" in msg.content:
                compacted.append(msg)
            elif msg.role == "tool":
                tool_name = self._infer_tool_name(messages, i)
                compacted.append(Message(
                    role=msg.role,
                    content=f"[{tool_name}: result omitted for context management]",
                    tool_call_id=msg.tool_call_id,
                    name=msg.name,
                ))
            elif msg.role == "assistant":
                compacted.append(Message(
                    role=msg.role,
                    content=(msg.content or "")[:100] + "..." if msg.content and len(msg.content) > 100 else msg.content,
                    tool_calls=msg.tool_calls,
                    reasoning_content=None,
                ))
            else:
                compacted.append(msg)

        self.last_input_tokens = 0
        logger.warning(
            f"Emergency compaction: {len(messages)} → {len(compacted)} msgs, "
            f"protected from idx {protect_from}"
        )
        return compacted

    def _infer_tool_name(self, messages: list[Message], tool_result_idx: int) -> str:
        """Walk backwards from a tool result to find which tool_call it belongs to."""
        tool_call_id = messages[tool_result_idx].tool_call_id
        if not tool_call_id:
            return "unknown"
        for i in range(tool_result_idx - 1, -1, -1):
            msg = messages[i]
            if msg.role == "assistant" and msg.tool_calls:
                for tc in msg.tool_calls:
                    tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                    tc_name = (tc.get("function", {}).get("name") if isinstance(tc, dict)
                               else getattr(tc, "name", "unknown"))
                    if tc_id == tool_call_id:
                        return tc_name
        return "unknown"

    def _summarize_tool_result(self, tool_name: str, content: str) -> str:
        """Produce a terse deterministic summary of a tool result."""
        if tool_name == "read_file":
            return self._summarize_read_file(content)
        if tool_name == "grep":
            return self._summarize_grep(content)
        if tool_name == "list_dir":
            return self._summarize_list_dir(content)
        if tool_name == "glob":
            return self._summarize_glob(content)
        # Fallback
        preview = content[:100].replace("\n", " ")
        return f"[{tool_name}: {preview}...]"

    def _summarize_read_file(self, content: str) -> str:
        try:
            data = json.loads(content)
            if isinstance(data, dict):
                path = data.get("path", "?")
                total = data.get("total_lines", "?")
                return f"[read_file: {path} -- {total} lines]"
        except (json.JSONDecodeError, TypeError):
            pass
        line_count = content.count("\n") + 1
        # Try to extract path from first line
        first_line = content.split("\n")[0][:80]
        return f"[read_file: {first_line}... -- ~{line_count} lines]"

    def _summarize_grep(self, content: str) -> str:
        try:
            data = json.loads(content)
            if isinstance(data, dict):
                pattern = data.get("pattern", "?")
                matches = data.get("matches", [])
                # Don't count truncation note entries
                count = sum(1 for m in matches if isinstance(m, dict) and "note" not in m)
                if data.get("truncated"):
                    return f"[grep: '{pattern}' -- {count}+ matches (truncated)]"
                return f"[grep: '{pattern}' -- {count} matches]"
        except (json.JSONDecodeError, TypeError):
            pass
        match_count = content.count("\n") + 1
        return f"[grep: ~{match_count} result lines]"

    def _summarize_list_dir(self, content: str) -> str:
        try:
            data = json.loads(content)
            if isinstance(data, dict):
                path = data.get("path", "?")
                entries = data.get("entries", [])
                count = sum(1 for e in entries if isinstance(e, dict) and "note" not in e)
                if data.get("truncated"):
                    return f"[list_dir: {path} -- {count}+ entries (truncated)]"
                return f"[list_dir: {path} -- {count} entries]"
        except (json.JSONDecodeError, TypeError):
            pass
        entry_count = content.count("\n") + 1
        return f"[list_dir: ~{entry_count} entries]"

    def _summarize_glob(self, content: str) -> str:
        try:
            data = json.loads(content)
            if isinstance(data, dict):
                pattern = data.get("pattern", "?")
                matches = len(data.get("matches", []))
                return f"[glob: '{pattern}' -- {matches} files]"
        except (json.JSONDecodeError, TypeError):
            pass
        return f"[glob: {content[:80]}...]"
