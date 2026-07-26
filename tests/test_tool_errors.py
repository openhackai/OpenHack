"""A tool raising (e.g. missing required arg from truncated tool-call JSON) must
become a recoverable error result, not kill the whole agent turn."""

import asyncio

from openhack.agents.base import BaseAgent
from openhack.agents.llm import LLMResponse, Message, ToolCall
from openhack.agents.session import Session
from openhack.tools.registry import ToolRegistry


class _ToolThenDoneLLM:
    """Turn 1: call run_command with NO args (mimics truncated/blank JSON args).
    Turn 2: return plain text so the loop ends."""

    model = "grok-4.5"

    def __init__(self):
        self.turn = 0

    async def chat(self, *args, **kwargs):
        self.turn += 1
        if self.turn == 1:
            return LLMResponse(
                content=None,
                tool_calls=[ToolCall(id="1", name="run_command", arguments={})],
                usage={"total_tokens": 1, "input_tokens": 1, "output_tokens": 0},
                cost=0.0,
            )
        return LLMResponse(
            content="recovered",
            tool_calls=[],
            usage={"total_tokens": 1, "input_tokens": 1, "output_tokens": 0},
            cost=0.0,
        )


class _Agent(BaseAgent):
    name = "t"

    def get_system_prompt(self, context: dict) -> str:
        return "x"

    def get_tools(self):
        return self.tools.get_all_tool_definitions()


def test_tool_exception_becomes_error_result_not_crash(tmp_path):
    session = Session(target_dir=str(tmp_path))
    tools = ToolRegistry(target_dir=tmp_path, include_agent_tools=True, session=session)
    agent = _Agent(_ToolThenDoneLLM(), tools, session)

    # Must NOT raise TypeError('missing command') — the loop should recover.
    result = asyncio.run(agent.run("go"))
    assert result.get("response") == "recovered"

    # The failed tool call was recorded as an error result the model could see.
    errs = [
        e for e in session.trace
        if e.event_type == "tool_result"
        and isinstance(e.tool_output, dict) and "error" in e.tool_output
    ]
    assert errs, "expected an error tool_result"
    assert "run_command" in errs[0].tool_output["error"]
