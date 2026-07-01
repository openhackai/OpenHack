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
from .recon import ReconTools
from .oob import OOBTools
from .browser import BrowserTools


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
            self.recon_tools = ReconTools()
            self.oob_tools = OOBTools()
            self.browser_tools = BrowserTools(evidence_dir=target_dir / ".openhack-evidence")
            self._tool_sources += [
                self.shell_tools, self.security_tools, self.mailbox_tools,
                self.recon_tools, self.oob_tools, self.browser_tools,
            ]

        self._tool_handlers = {}
        self._async_handlers = {}
        self._register_tools()

    def _register_tools(self):
        for source in self._tool_sources:
            is_async = getattr(source, "is_async", False)
            for tool in source.get_tool_definitions():
                if is_async:
                    self._async_handlers[tool["name"]] = source.execute_tool_async
                else:
                    self._tool_handlers[tool["name"]] = source.execute_tool

    def get_all_tool_definitions(self) -> list[dict]:
        tools = []
        for source in self._tool_sources:
            tools.extend(source.get_tool_definitions())
        return tools

    def is_async_tool(self, name: str) -> bool:
        return name in self._async_handlers

    def execute_tool(self, name: str, arguments: dict) -> Any:
        if name in self._tool_handlers:
            return self._tool_handlers[name](name, arguments)
        if name in self._async_handlers:
            return {"error": f"{name} is an async tool; call execute_tool_async"}
        return {"error": f"Unknown tool: {name}"}

    async def execute_tool_async(self, name: str, arguments: dict) -> Any:
        if name in self._async_handlers:
            return await self._async_handlers[name](name, arguments)
        return self.execute_tool(name, arguments)
