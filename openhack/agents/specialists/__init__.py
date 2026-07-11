"""
Per-vuln-class specialist exploiter agents.

Each specialist is a focused `InteractiveAgent` (see `base_specialist.SpecialistAgent`)
with a class-specific exploitation playbook. `build_specialist` is the single factory
used by BOTH entry paths: the black-box `hack` dispatch tool and the white-box scan
exploit swarm — so both get identical playbooks/toolsets; only the target base URL
and solo-vs-fan-out differ.
"""

from .base_specialist import SpecialistAgent
from .xss import XSSSpecialist
from .injection import InjectionSpecialist
from .ssrf import SSRFSpecialist
from .ssti import SSTISpecialist
from .auth import AuthSpecialist
from .blind import BlindOOBSpecialist

SPECIALIST_REGISTRY: dict[str, type[SpecialistAgent]] = {
    "xss": XSSSpecialist,
    "injection": InjectionSpecialist,
    "ssrf": SSRFSpecialist,
    "ssti": SSTISpecialist,
    "auth": AuthSpecialist,
    "blind": BlindOOBSpecialist,
}

# (matched-substring, specialist-key) — first hit wins. Covers XBOW tags and the
# canonical categories in openhack/categories.py. Anything unmatched → "injection"
# (the most general offensive playbook).
_CLASSIFY_RULES: list[tuple[tuple[str, ...], str]] = [
    (("blind", "out-of-band", "oob"), "blind"),
    (("xss", "cross-site scripting", "cross site scripting"), "xss"),
    (("ssti", "template injection", "template inject"), "ssti"),
    (("ssrf", "server-side request", "server side request"), "ssrf"),
    (("idor", "auth", "authz", "authn", "authorization", "authentication",
      "jwt", "access control", "access-control", "privilege", "default cred",
      "default_cred", "broken access", "session"), "auth"),
    (("sqli", "sql injection", "sql-injection", "nosql", "command injection",
      "command_injection", "cmd inject", "code injection", "rce", "remote code",
      "injection"), "injection"),
]


def classify_vuln_class(category_or_notes: str) -> str:
    """Map a vuln tag/category/free-text hint to one of the specialist keys."""
    if not category_or_notes:
        return "injection"
    text = str(category_or_notes).lower()
    for needles, key in _CLASSIFY_RULES:
        if any(n in text for n in needles):
            return key
    return "injection"


def build_specialist(
    vuln_class: str,
    target_dir,
    session,
    model=None,
    base_url: str = "",
):
    """Construct a specialist agent for the given class.

    Builds the specialist its own LLM client + ToolRegistry (sharing the session so
    findings land in one place). XSS-class specialists get a registry with the
    stateful browser; `base_url` lets the white-box swarm point the browser/tools at
    the live sandbox (default "" → absolute URLs pass through for black-box).
    """
    from pathlib import Path
    from openhack.agents.llm import LLMClient
    from openhack.tools.registry import ToolRegistry

    key = classify_vuln_class(vuln_class)
    cls = SPECIALIST_REGISTRY[key]
    llm = LLMClient(model=model, prompt_cache_key=f"openhack-specialist-{key}")
    tools = ToolRegistry(
        target_dir=Path(target_dir),
        include_agent_tools=True,
        session=session,
        include_stateful_browser=cls.NEEDS_STATEFUL_BROWSER,
        browser_base_url=base_url,
    )
    return cls(llm=llm, tools=tools, session=session)


__all__ = [
    "SpecialistAgent", "XSSSpecialist", "InjectionSpecialist", "SSRFSpecialist",
    "SSTISpecialist", "AuthSpecialist", "BlindOOBSpecialist",
    "SPECIALIST_REGISTRY", "classify_vuln_class", "build_specialist",
]
