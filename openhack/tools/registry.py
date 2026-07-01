"""
Tool registry for vulnerability scanning.
Manages all available tools and dispatches tool calls.
"""

from pathlib import Path
from typing import Any

from .filesystem import FileSystemTools
from .nextjs import NextJSTools
from .ast_tools import ASTTools
from .shell import ShellTools
from .security_tools import SecurityTools
from .mailbox import MailboxTools


class ToolRegistry:
    """Registry that manages all scanning tools and their execution.

    By default it exposes the read-only, target-jailed scanning tools used by
    the vuln-scan pipeline. Pass ``include_agent_tools=True`` to also expose the
    interactive hacking toolkit (shell execution, SCA/secret scanners,
    disposable mailbox) used by the interactive agent.
    """

    def __init__(self, target_dir: Path, include_agent_tools: bool = False):
        self.target_dir = target_dir
        self.fs_tools = FileSystemTools(target_dir)
        self.nextjs_tools = NextJSTools(self.fs_tools)
        self.ast_tools = ASTTools(self.fs_tools)

        self.include_agent_tools = include_agent_tools
        self._tool_sources = [self.fs_tools, self.nextjs_tools, self.ast_tools]

        if include_agent_tools:
            self.shell_tools = ShellTools(workdir=target_dir)
            self.security_tools = SecurityTools(workdir=target_dir)
            self.mailbox_tools = MailboxTools()
            self._tool_sources += [self.shell_tools, self.security_tools, self.mailbox_tools]

        self._tool_handlers = {}
        self._register_tools()

    def _register_tools(self):
        for source in self._tool_sources:
            for tool in source.get_tool_definitions():
                self._tool_handlers[tool["name"]] = source.execute_tool

    def get_all_tool_definitions(self) -> list[dict]:
        tools = []
        for source in self._tool_sources:
            tools.extend(source.get_tool_definitions())
        return tools

    def is_async_tool(self, name: str) -> bool:
        return False

    def execute_tool(self, name: str, arguments: dict) -> Any:
        if name not in self._tool_handlers:
            return {"error": f"Unknown tool: {name}"}
        return self._tool_handlers[name](name, arguments)

    async def execute_tool_async(self, name: str, arguments: dict) -> Any:
        return self.execute_tool(name, arguments)
