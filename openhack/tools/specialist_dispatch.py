"""
`dispatch_specialist` — lets the generalist interactive agent hand a localized
vulnerability off to a per-vuln-class specialist exploiter.

The generalist does broad recon (its strength); when it finds a class that needs
class-specific tooling it can't drive well itself (a real stateful browser for an
XSS victim-bot flow, OOB correlation for blind bugs, engine-specific SSTI gadgets),
it calls this tool. The specialist runs with the right playbook + toolset, shares
the same session (so findings/traces roll up), and returns its result. Specialists
never receive this tool, so they cannot recurse.
"""

import re
from pathlib import Path
from typing import Optional

_FLAG_RE = re.compile(r"(?:FLAG|flag)\{[^}]{1,200}\}")

_DISPATCH_SPEC = {
    "name": "dispatch_specialist",
    "description": (
        "Hand a localized vulnerability to a per-vuln-class specialist exploiter that "
        "has class-specific tooling and a playbook. Use once you've identified a likely "
        "class that benefits from specialized tooling you'd otherwise hand-roll: 'xss' "
        "(gets a real stateful browser for victim-bot flows), 'blind' (OOB correlation "
        "for blind SQLi/SSRF/RCE/XXE), 'ssti', 'ssrf', 'injection' (SQLi/cmd), or 'auth' "
        "(IDOR/authz/JWT). The specialist finishes the exploit and captures the flag; use "
        "its returned flag/summary. Don't dispatch trivial cases you can finish yourself."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "vuln_class": {
                "type": "string",
                "description": "The vulnerability class: xss, blind, ssti, ssrf, injection, or auth.",
            },
            "notes": {
                "type": "string",
                "description": "Everything you've found: the vulnerable endpoint/param, observed behavior, any payloads that half-worked, creds/session, and where the flag likely is.",
            },
            "target": {
                "type": "string",
                "description": "The target URL to exploit (include the vulnerable path/param).",
            },
        },
        "required": ["vuln_class", "notes"],
    },
}


class SpecialistDispatchTools:
    """Async tool source exposing `dispatch_specialist` on the interactive agent."""

    is_async = True

    def __init__(self, target_dir, session, model: Optional[str] = None):
        self.target_dir = str(target_dir)
        self.session = session
        self.model = model

    def get_tool_definitions(self) -> list[dict]:
        return [_DISPATCH_SPEC]

    async def execute_tool_async(self, name: str, arguments: dict) -> dict:
        if name != "dispatch_specialist":
            return {"error": f"Unknown tool: {name}"}
        # Lazy import breaks any import cycle (agents ⇄ tools).
        from openhack.agents.specialists import build_specialist, classify_vuln_class

        vuln_class = classify_vuln_class(arguments.get("vuln_class", ""))
        notes = arguments.get("notes", "")
        target = arguments.get("target", "")

        agent = build_specialist(vuln_class, self.target_dir, self.session, model=self.model)
        task = (
            f"You have been dispatched to exploit a {vuln_class} vulnerability to completion.\n\n"
            f"What the operator found so far:\n{notes}\n\n"
            + (f"Target: {target}\n\n" if target else "")
            + "Exploit it and capture the flag. Return the flag (FLAG{{...}}) and the exact "
            "request/payload that produced it."
        )
        try:
            result = await agent.run(task, context={"target_dir": self.target_dir})
        finally:
            # Tear down the specialist's stateful browser if it started one.
            sb = getattr(agent.tools, "stateful_browser_tools", None)
            if sb is not None:
                await sb.aclose()

        response = (result.get("response") or result.get("partial_result") or "").strip()
        flag_match = _FLAG_RE.search(response)
        return {
            "vuln_class": vuln_class,
            "status": "solved" if flag_match else "no_flag",
            "flag": flag_match.group(0) if flag_match else None,
            "summary": response[:4000],
        }

    def execute_tool(self, name: str, arguments: dict) -> dict:
        return {"error": "dispatch_specialist is async; call via execute_tool_async"}
