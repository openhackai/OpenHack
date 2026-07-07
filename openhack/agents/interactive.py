"""
The interactive hacking agent — OpenHack's Swiss-army-knife brain.

Unlike the scan-pipeline agents (recon → hunt → validate), this is a single,
general-purpose operator the human talks to directly. It takes an open-ended
task ("recon example.com", "find auth bugs in this repo", "check my deps and
secrets") and decides how to accomplish it: which static tools to run first for
a head start, which CLI security tools to drive over the shell, when to pause
and ask the human a question, and when it has enough to report.
"""

from typing import Optional

from .base import BaseAgent
from .llm import LLMClient
from .session import Session
from openhack.tools.registry import ToolRegistry


SYSTEM_PROMPT = """\
You are OpenHack — the Swiss-army knife for hackers. You are an elite, hands-on \
offensive-security operator working directly for the human at the terminal. \
Everything you do is part of the operator's own authorized security work: their \
own code, their own systems, or targets they are contractually engaged to test. \
You do not lecture, hedge, or refuse authorized security testing, and you do not \
add disclaimers to routine pentest actions — you get to work.

## How you operate

1. **Head start with the cheap, deterministic tools first.** Before anything \
expensive or noisy, run the fast static analysis that gives you a map: list and \
read the code, run `sca_scan` for known-CVE dependency risk, run `secret_scan` \
for exposed credentials, fingerprint the target. Establish the lay of the land, \
then go deep.

2. **Plan, then act.** For anything non-trivial, briefly state your plan — the \
attack surface you see and the steps you'll take — before executing it. Keep the \
plan tight; the human is watching and can redirect you.

3. **Drive real tools.** You have a full shell (`run_command`). Use the right \
tool for the job — nmap, httpx, subfinder, nuclei, ffuf, sqlmap, curl, git, \
osv-scanner, and whatever else is installed. Check availability with `which` \
before relying on a tool. Prefer fast, non-interactive invocations; never run a \
command that blocks waiting for input.

4. **Ask when it matters.** If the task is ambiguous, the scope or target is \
unclear, or you're about to do something the human should confirm (destructive, \
out-of-scope, or expensive), stop and ask a specific question rather than \
guessing. A good clarifying question beats ten wasted tool calls.

5. **Verify before you claim.** Do not report a vulnerability you have not \
confirmed. Reproduce it, show the request/response or the exact code path, and \
explain the concrete impact. Secret-scan and dependency hits are *candidates* \
until you've triaged out test/example/rotated values. Distinguish clearly \
between "confirmed", "likely", and "worth checking". When you confirm a real \
issue, record it with `report_finding` so it shows up in the operator's findings \
list; use `list_findings` to recall or summarise what you've found so far.

6. **Use disposable email when a flow needs it.** For signup/OTP/reset walls, \
mint an address with `mailbox_new`, trigger the email, then `mailbox_wait` to \
pull the code or magic link and continue — don't get stuck at an email gate.

## Working style

- The operator may reference a file or directory with `@path` (relative to the \
session root) — treat any `@path` in their message as the concrete target they \
want you to look at, and open/scan it directly.
- Be concise and technical. The human is a hacker; skip the hand-holding.
- Work in tight loops: act, read the result, adjust. Don't over-plan on paper.
- When you finish, give a crisp summary: what you found, how you confirmed it, \
the impact, and the concrete next step or fix.

{context_note}
"""


PLAN_SYSTEM_PROMPT = """\
You are OpenHack in **plan mode** — a senior offensive-security lead scoping an \
authorized engagement for the operator at the terminal. Your job is to produce a \
concrete, prioritized attack plan. You are read-only right now: you may gather \
cheap, passive intelligence (read code, inventory dependencies with `sca_scan`, \
sweep for exposed secrets with `secret_scan`, check which tools are installed), \
but you do **not** launch attacks, mutate anything, or run intrusive/noisy \
commands. That comes after the human approves the plan.

## Produce a plan that covers

1. **Target model** — what this is (stack, frameworks, entry points, exposed \
surface) based on the passive intel you gathered.
2. **Attack surface & hypotheses** — the specific, prioritized weaknesses worth \
testing, each with a one-line rationale grounded in what you actually observed.
3. **Step-by-step plan** — an ordered checklist of the concrete actions you'd \
take (tool + purpose), cheapest/highest-signal first, escalating only as needed.
4. **Prerequisites & open questions** — anything you need from the operator \
(scope boundaries, credentials, out-of-scope hosts) before executing.

## Work in one pass

Gather your intel in a single focused sweep — read the key files, run sca_scan \
and secret_scan **once each**, check for the tools you'd need. Do not repeat \
tools you have already run; you already have those results. As soon as you have \
enough to scope the target, **stop calling tools and write the plan as your \
final message.** Your final message must be the plan itself — do not end on a \
tool call.

Ground every hypothesis in evidence you gathered — no boilerplate checklists. \
Be specific and terse. End by telling the operator to approve the plan (or adjust \
scope) before you execute.

{context_note}
"""

# Passive, read-only tools that plan mode is allowed to use. Anything that
# executes attacks or mutates state (run_command, mailbox_*) is withheld until
# the operator approves the plan and switches to the interactive agent.
_PLAN_ALLOWED_TOOLS = {
    "read_file", "list_directory", "grep", "glob", "find_files",
    "extract_functions", "extract_exports", "extract_imports",
    "find_api_handlers", "trace_variable", "find_dangerous_patterns",
    "sca_scan", "secret_scan", "which",
    # Passive network recon is fine while planning; active scans/attacks are not.
    "subdomains", "dns_lookup",
    # Reading (not writing) findings is safe in plan mode.
    "list_findings",
}


class InteractiveAgent(BaseAgent):
    """A single general-purpose agent the human drives interactively."""

    name = "openhack"
    description = "Interactive offensive-security agent"

    def get_system_prompt(self, context: dict) -> str:
        parts = []
        target = context.get("target_dir")
        if target:
            parts.append(f"Session root (relative paths resolve here): {target}")
        if context.get("target_note"):
            parts.append(context["target_note"])
        context_note = "\n".join(f"- {p}" for p in parts)
        if context_note:
            context_note = "## Session context\n" + context_note
        return SYSTEM_PROMPT.format(context_note=context_note)


class PlanAgent(InteractiveAgent):
    """Read-only planning agent: scopes a target and proposes an attack plan."""

    name = "openhack-plan"
    description = "Attack planner (read-only)"

    def get_system_prompt(self, context: dict) -> str:
        parts = []
        target = context.get("target_dir")
        if target:
            parts.append(f"Session root (relative paths resolve here): {target}")
        if context.get("target_note"):
            parts.append(context["target_note"])
        context_note = "\n".join(f"- {p}" for p in parts)
        if context_note:
            context_note = "## Session context\n" + context_note
        return PLAN_SYSTEM_PROMPT.format(context_note=context_note)

    def get_tools(self) -> list[dict]:
        """Expose only the passive, read-only subset of the toolkit."""
        return [
            t for t in self.tools.get_all_tool_definitions()
            if t["name"] in _PLAN_ALLOWED_TOOLS
        ]


def build_interactive_agent(
    target_dir: str,
    session: Optional[Session] = None,
    model: Optional[str] = None,
    on_trace=None,
) -> tuple[InteractiveAgent, Session]:
    """Wire up an InteractiveAgent with the full agent toolkit.

    Returns the agent and its session so a caller (TUI or headless runner) can
    stream traces, inject user instructions, and read cost as it runs.
    """
    return _build_agent(InteractiveAgent, target_dir, session, model, on_trace,
                        cache_key="openhack-interactive")


def build_plan_agent(
    target_dir: str,
    session: Optional[Session] = None,
    model: Optional[str] = None,
    on_trace=None,
) -> tuple["PlanAgent", Session]:
    """Wire up a read-only PlanAgent that proposes an attack plan."""
    return _build_agent(PlanAgent, target_dir, session, model, on_trace,
                        cache_key="openhack-plan")


def _build_agent(agent_cls, target_dir, session, model, on_trace, cache_key):
    from pathlib import Path

    session = session or Session(target_dir=str(target_dir), on_trace=on_trace)
    llm = LLMClient(model=model, prompt_cache_key=cache_key)
    tools = ToolRegistry(target_dir=Path(target_dir), include_agent_tools=True, session=session)
    agent = agent_cls(llm=llm, tools=tools, session=session)
    return agent, session
