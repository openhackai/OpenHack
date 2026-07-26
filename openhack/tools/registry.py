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
from .web import WebTools


class ToolRegistry:
    """Registry that manages all scanning tools and their execution.

    By default it exposes the read-only, target-jailed scanning tools used by
    the vuln-scan pipeline. Pass ``include_agent_tools=True`` to also expose the
    interactive hacking toolkit (shell execution, SCA/secret scanners,
    disposable mailbox) used by the interactive agent.
    """

    def __init__(self, target_dir: Path, include_agent_tools: bool = False, session=None,
                 include_stateful_browser: bool = False, browser_base_url: str = "",
                 shells=None):
        self.target_dir = target_dir
        self.fs_tools = FileSystemTools(target_dir)
        self.nextjs_tools = NextJSTools(self.fs_tools)
        self.ast_tools = ASTTools(self.fs_tools)

        self.include_agent_tools = include_agent_tools
        self._tool_sources = [self.fs_tools, self.nextjs_tools, self.ast_tools]

        if include_agent_tools:
            self.shell_tools = ShellTools(workdir=target_dir, session=session, shells=shells)
            self.security_tools = SecurityTools(workdir=target_dir, session=session)
            self.mailbox_tools = MailboxTools(session=session)
            self.recon_tools = ReconTools(session=session)
            self.oob_tools = OOBTools()
            self.browser_tools = BrowserTools(evidence_dir=target_dir / ".openhack-evidence")
            self.web_tools = WebTools()
            self._tool_sources += [
                self.shell_tools, self.security_tools, self.mailbox_tools,
                self.recon_tools, self.oob_tools, self.browser_tools,
                self.web_tools,
            ]
            # Stateful (persistent-session) browser — opt-in, for specialist
            # exploiters (e.g. XSS victim-bot flows). Off by default so the
            # generalist agent's toolset is unchanged.
            if include_stateful_browser:
                from .stateful_browser import StatefulBrowserTools
                self.stateful_browser_tools = StatefulBrowserTools(
                    base_url=browser_base_url,
                    evidence_dir=target_dir / ".openhack-evidence",
                )
                self._tool_sources.append(self.stateful_browser_tools)
            # Findings tools need the session to read/write findings.
            if session is not None:
                from .findings import FindingsTools
                self.findings_tools = FindingsTools(session)
                self._tool_sources.append(self.findings_tools)

        self._tool_handlers = {}
        self._async_handlers = {}
        self._register_tools()

    def _register_tools(self):
        for source in self._tool_sources:
            self._register_source(source)

    def _register_source(self, source):
        is_async = getattr(source, "is_async", False)
        for tool in source.get_tool_definitions():
            if is_async:
                self._async_handlers[tool["name"]] = source.execute_tool_async
            else:
                self._tool_handlers[tool["name"]] = source.execute_tool

    def attach_source(self, source) -> None:
        """Add a tool source after construction (e.g. specialist dispatch, which
        needs the built agent's model). Additive — leaves existing tools intact."""
        self._tool_sources.append(source)
        self._register_source(source)

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
