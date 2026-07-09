"""
Non-TUI runner for the interactive hacking agent.

This is the plain-terminal front end to :class:`InteractiveAgent` — the mode
that works over SSH, in CI, and when piped to other tools. It streams the
agent's thinking and tool activity as readable lines, then prints the final
report. The rich TUI drives the same agent; this is the lowest-common-
denominator surface so nothing is locked behind a full-screen UI.

Entry points:
    run_task(task, target_dir)   — run a single task and exit
    run_repl(target_dir)         — an interactive prompt loop
"""

import asyncio
import json
import sys
from pathlib import Path
from typing import Optional

# ANSI styling — Signal Green is the OpenHack accent. Disabled when not a TTY.
_TTY = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    if not _TTY:
        return text
    return f"\033[{code}m{text}\033[0m"


_GREEN = "38;2;0;185;126"
_MUTED = "38;2;126;135;132"
_BOLD = "1"
_RED = "38;2;234;106;100"


def _fmt_tool_input(tool_input: Optional[dict]) -> str:
    if not tool_input:
        return ""
    # Show the most useful arg inline (command / path / to), else compact JSON.
    for key in ("command", "path", "to", "tool", "pattern"):
        if key in tool_input and isinstance(tool_input[key], str):
            val = tool_input[key]
            return val if len(val) <= 100 else val[:97] + "..."
    try:
        blob = json.dumps(tool_input)
    except (TypeError, ValueError):
        blob = str(tool_input)
    return blob if len(blob) <= 100 else blob[:97] + "..."


def _make_trace_printer(state: Optional[dict] = None):
    """Return an on_trace callback that renders agent activity to the terminal.

    If ``state`` is given, the last substantive 'thinking' text is stashed in
    ``state['last_text']`` so the caller can fall back to it when the agent's
    final response comes back empty (it sometimes ends a turn with tool calls
    only and no closing prose).
    """
    def on_trace(entry) -> None:
        etype = entry.event_type
        if etype == "thinking":
            text = (entry.content or "").strip()
            if text:
                if state is not None:
                    state["last_text"] = text
                print(_c(_MUTED, "  ·") + " " + text)
        elif etype == "tool_call":
            arg = _fmt_tool_input(entry.tool_input)
            label = _c(_GREEN, f"→ {entry.tool_name}")
            print(f"  {label} " + _c(_MUTED, arg))
        elif etype == "tool_result":
            # Keep results quiet in the stream; the agent reasons over them and
            # will surface what matters. Show a faint acknowledgement only.
            out = entry.tool_output
            note = ""
            if isinstance(out, dict):
                if "error" in out:
                    note = _c(_RED, f"error: {out['error']}")
                elif "exit_code" in out:
                    note = _c(_MUTED, f"exit {out['exit_code']}")
                elif "count" in out:
                    note = _c(_MUTED, f"{out['count']} results")
            if note:
                print(f"    {note}")
    return on_trace


def _print_banner(target_dir: str, model: str) -> None:
    print()
    print(_c(_GREEN, _c(_BOLD, "  ⏚ OpenHack")) + _c(_MUTED, "  · the swiss-army knife for hackers"))
    print(_c(_MUTED, f"    root {target_dir}   model {model}"))
    print()


async def _run_once(agent, session, task: str) -> dict:
    result = await agent.run(task, context={"target_dir": session.target_dir})
    return result


def _print_result(result: dict, session, fallback: str = "") -> None:
    print()
    response = (result.get("response") or result.get("partial_result") or "").strip()
    streamed = fallback.strip()
    if result.get("error"):
        print(_c(_RED, f"  {result['error']}"))

    if response and response == streamed:
        # The final answer was already shown live as it streamed — don't repeat it.
        pass
    elif response:
        print(response)
    elif streamed:
        # The agent ended on a tool call with no closing prose; recover its last
        # substantive message so the operator never gets a blank screen.
        print(streamed)
    else:
        print(_c(_MUTED, "  (no textual output — check the tool activity above)"))
    print()
    cost = getattr(session, "total_cost", 0.0)
    tokens = getattr(session, "total_tokens", 0)
    print(_c(_MUTED, f"  {tokens:,} tokens · ${cost:.4f}"))
    print()


def run_task(task: str, target_dir: Optional[str] = None, model: Optional[str] = None) -> dict:
    """Run a single task to completion and print the report. Returns the result."""
    from openhack.agents.interactive import build_interactive_agent

    target = str(Path(target_dir).resolve()) if target_dir else str(Path.cwd())
    state: dict = {}
    agent, session = build_interactive_agent(
        target_dir=target, model=model, on_trace=_make_trace_printer(state)
    )
    _print_banner(target, agent.llm.model)
    print(_c(_BOLD, "  " + task))
    print()
    try:
        result = asyncio.run(_run_once(agent, session, task))
    except KeyboardInterrupt:
        session.cancel()
        print(_c(_MUTED, "\n  interrupted"))
        _persist_run(session, task, target, "cancelled")
        return {"error": "interrupted"}
    _print_result(result, session, fallback=state.get("last_text", ""))
    _persist_run(session, task, target, "completed")
    return result


def _persist_run(session, task: str, target: str, status: str) -> None:
    """Write a full structured trace of the run (every tool input/output, cost,
    findings) to ~/.openhack/scans/<id>.json for later review. Never fails the run."""
    try:
        import json
        report_dir = Path.home() / ".openhack" / "scans"
        report_dir.mkdir(parents=True, exist_ok=True)

        def _trace(e):
            out = e.tool_output
            if out is not None and not isinstance(out, (dict, list, int, float, bool)):
                s = str(out)
                out = s if len(s) <= 8000 else s[:8000] + "…"
            return {
                "timestamp": e.timestamp, "agent": e.agent, "event_type": e.event_type,
                "content": e.content, "tool_name": e.tool_name,
                "tool_input": e.tool_input, "tool_output": out,
            }

        report = {
            "version": 2, "kind": "hack", "task": task, "scan_id": session.id,
            "target_dir": target, "status": status,
            "cost": session.get_cost_breakdown(),
            "findings": [f.to_dict() for f in session.findings],
            "trace": [_trace(e) for e in session.trace],
        }
        path = report_dir / f"{session.id}.json"
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w") as fp:
            json.dump(report, fp, indent=2, default=str, ensure_ascii=False)
        import os as _os
        _os.replace(tmp, path)
    except Exception:
        pass


def run_plan(objective: str, target_dir: Optional[str] = None, model: Optional[str] = None) -> dict:
    """Produce a read-only attack plan for an objective/target. Returns the result."""
    from openhack.agents.interactive import build_plan_agent

    target = str(Path(target_dir).resolve()) if target_dir else str(Path.cwd())
    state: dict = {}
    agent, session = build_plan_agent(
        target_dir=target, model=model, on_trace=_make_trace_printer(state)
    )
    _print_banner(target, agent.llm.model)
    print(_c(_BOLD, "  plan: " + objective))
    print(_c(_MUTED, "  (read-only — no attacks run until you approve)"))
    print()
    try:
        result = asyncio.run(_run_once(agent, session, objective))
    except KeyboardInterrupt:
        session.cancel()
        print(_c(_MUTED, "\n  interrupted"))
        return {"error": "interrupted"}
    _print_result(result, session, fallback=state.get("last_text", ""))
    return result


def run_repl(target_dir: Optional[str] = None, model: Optional[str] = None) -> None:
    """Interactive prompt loop. Each line is a task for the same session/agent."""
    from openhack.agents.interactive import build_interactive_agent

    target = str(Path(target_dir).resolve()) if target_dir else str(Path.cwd())
    state: dict = {}
    agent, session = build_interactive_agent(
        target_dir=target, model=model, on_trace=_make_trace_printer(state)
    )
    _print_banner(target, agent.llm.model)
    print(_c(_MUTED, "  Type a task and press enter. Ctrl-D or /exit to quit."))
    print()

    while True:
        try:
            task = input(_c(_GREEN, "  ▌ ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not task:
            continue
        if task in ("/exit", "/quit", "exit", "quit"):
            break
        state.clear()
        try:
            result = asyncio.run(_run_once(agent, session, task))
        except KeyboardInterrupt:
            print(_c(_MUTED, "\n  interrupted — ask something else or /exit"))
            continue
        _print_result(result, session, fallback=state.get("last_text", ""))
