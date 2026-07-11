"""
Base class for per-vuln-class specialist exploiters.

A specialist is just an `InteractiveAgent` with (1) a class-specific exploitation
playbook appended to the standard operator system prompt and (2) optionally a
narrowed toolset. It inherits the entire `BaseAgent._agent_loop` — anti-thrash
ledger, compaction, streaming, findings, progress-aware stop — so there is zero
loop or tool-dispatch duplication. This mirrors how `PlanAgent` specializes
`InteractiveAgent` by overriding `get_system_prompt`/`get_tools`.
"""

from openhack.agents.interactive import InteractiveAgent


class SpecialistAgent(InteractiveAgent):
    """An InteractiveAgent focused on a single vulnerability class."""

    name = "openhack-specialist"
    description = "Vuln-class exploitation specialist"

    # Overridden per class.
    vuln_class: str = "generic"
    PLAYBOOK: str = ""
    # None = full agent toolkit; else an allow-list of tool names (like PlanAgent).
    ALLOWED_TOOLS = None
    # If True, build_specialist gives this agent a registry with the stateful
    # browser (navigate/fill/click/… persistent session) for real victim-bot flows.
    NEEDS_STATEFUL_BROWSER: bool = False

    def get_system_prompt(self, context: dict) -> str:
        # Reuse the full InteractiveAgent operator prompt (+ session context_note),
        # then append this specialist's exploitation playbook.
        base = super().get_system_prompt(context)
        if self.PLAYBOOK:
            return base + "\n\n" + self.PLAYBOOK
        return base

    def get_tools(self) -> list[dict]:
        defs = self.tools.get_all_tool_definitions()
        if self.ALLOWED_TOOLS is not None:
            defs = [t for t in defs if t["name"] in self.ALLOWED_TOOLS]
        return defs
