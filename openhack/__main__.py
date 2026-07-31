"""
Entry point for OpenHack.

Usage:
  openhack [path]                     Launch the interactive TUI (path defaults to .)
  openhack --scan [path]              Scan a repository (headless, defaults to .)
  openhack --hack "<task>" [path]     Run one hacking task headlessly (no TUI)
  openhack --plan "<objective>" [path] Draft a read-only attack plan (no TUI)
  openhack --agent [path]             Interactive agent prompt loop (no TUI)
  openhack --sessions                 List all saved scan sessions
  openhack --resume <id>              Resume a previous scan session
  openhack --classify [path]          Classify frameworks and detect entry points
  openhack --login                    Log in to your OpenHack account
  openhack --setup                    Run the setup wizard
  openhack --showkey                  Print the raw bytes your terminal sends (key diagnostic)
  openhack --help                     Show usage
"""

import sys


def _cmd_scan():
    """Run a headless scan on a directory."""
    from pathlib import Path

    target_arg = sys.argv[2] if len(sys.argv) > 2 else "."
    target = Path(target_arg).resolve()

    if not target.is_dir():
        print(f"Error: '{target_arg}' is not a directory.")
        print("Usage: openhack --scan [path]")
        return

    from openhack.config import settings
    if not settings.openhack_api_key:
        print("Error: not logged in.")
        print("Run 'openhack --login' to set up your account, or set OPENHACK_API_KEY.")
        return

    import asyncio
    from openhack.headless_scan import run_headless_scan
    try:
        asyncio.run(run_headless_scan(str(target)))
    except KeyboardInterrupt:
        print()
    except Exception:
        pass


def _cmd_hack():
    """Run a single hacking task headlessly (no TUI)."""
    from pathlib import Path

    task = sys.argv[2] if len(sys.argv) > 2 else None
    if not task:
        print('Usage: openhack --hack "<task>" [path]')
        print('Example: openhack hack "check this repo for exposed secrets and vulnerable deps"')
        return

    target_arg = sys.argv[3] if len(sys.argv) > 3 else "."
    target = Path(target_arg).resolve()
    if not target.is_dir():
        target = Path.cwd()

    from openhack.config import settings
    if not settings.openhack_api_key:
        print("Error: not logged in.")
        print("Run 'openhack --login' to set up your account, or set OPENHACK_API_KEY.")
        return

    from openhack.interactive_runner import run_task
    try:
        run_task(task, target_dir=str(target))
    except KeyboardInterrupt:
        print()


def _cmd_plan():
    """Produce a read-only attack plan for a target/objective (no TUI)."""
    from pathlib import Path

    objective = sys.argv[2] if len(sys.argv) > 2 else None
    if not objective:
        print('Usage: openhack --plan "<objective>" [path]')
        print('Example: openhack plan "find auth and access-control bugs in this app"')
        return

    target_arg = sys.argv[3] if len(sys.argv) > 3 else "."
    target = Path(target_arg).resolve()
    if not target.is_dir():
        target = Path.cwd()

    from openhack.config import settings
    if not settings.openhack_api_key:
        print("Error: not logged in.")
        print("Run 'openhack --login' to set up your account, or set OPENHACK_API_KEY.")
        return

    from openhack.interactive_runner import run_plan
    try:
        run_plan(objective, target_dir=str(target))
    except KeyboardInterrupt:
        print()


def _cmd_agent():
    """Interactive agent prompt loop (no TUI)."""
    from pathlib import Path

    target_arg = sys.argv[2] if len(sys.argv) > 2 else "."
    target = Path(target_arg).resolve()
    if not target.is_dir():
        target = Path.cwd()

    from openhack.config import settings
    if not settings.openhack_api_key:
        print("Error: not logged in.")
        print("Run 'openhack --login' to set up your account, or set OPENHACK_API_KEY.")
        return

    from openhack.interactive_runner import run_repl
    try:
        run_repl(target_dir=str(target))
    except KeyboardInterrupt:
        print()


def _cmd_sessions():
    """List all saved scan sessions."""
    import json
    from pathlib import Path

    scans_dir = Path.home() / ".openhack" / "scans"
    if not scans_dir.exists():
        print("\nNo saved scans yet.")
        return

    reports = []
    for p in sorted(scans_dir.glob("*.json"), reverse=True):
        try:
            data = json.loads(p.read_text())
            reports.append(data)
        except (OSError, json.JSONDecodeError):
            continue

    print(f"\nSaved scans: {len(reports)}")
    for r in reports:
        scan_id = (r.get("scan_id") or "?")[:8]
        target = r.get("target_dir", "?")
        status = r.get("status", "?")
        findings = r.get("findings", [])
        started = r.get("started_at", "")[:16]
        print(f"  {scan_id}  {target}  [{status}]  {len(findings)} findings  {started}")


def _cmd_resume():
    """Reopen a saved session in the TUI — transcript restored, ready to continue."""
    from pathlib import Path

    session_id = sys.argv[2] if len(sys.argv) > 2 else None
    if not session_id:
        print("Usage: openhack --resume <session_id>")
        return

    scans_dir = Path.home() / ".openhack" / "scans"
    report_path = scans_dir / f"{session_id}.json"
    if not report_path.exists():
        matches = sorted(scans_dir.glob(f"{session_id}*.json"))
        if matches:
            report_path = matches[0]

    if not report_path.exists():
        print(f"Session {session_id} not found in ~/.openhack/scans/")
        print("List saved sessions with: openhack --sessions")
        return

    # Launch the TUI resumed on this session (the app hydrates the transcript
    # and, for agent sessions, rebuilds a continuable agent).
    _launch_tui(resume_session_id=report_path.stem)


def _cmd_classify():
    """Classify frameworks and detect entry points."""
    from pathlib import Path
    from openhack.tools.registry import ToolRegistry
    from openhack.framework_classifier import classify_frameworks
    from openhack.entry_points import detect_entry_points
    from openhack.scan_session import ScanSession
    import uuid

    target = sys.argv[2] if len(sys.argv) > 2 else "."
    tools = ToolRegistry(target_dir=Path(target))

    print(f"\nClassifying {target}...\n")
    classifications = classify_frameworks(tools.fs_tools)
    for c in classifications:
        print(f"  {c['root']} → {c['language']} [{', '.join(c['frameworks'])}]")

    print(f"\nDetecting entry points...")
    entry_points = detect_entry_points(tools.fs_tools, classifications)
    print(f"  {len(entry_points)} entry points found\n")

    sid = str(uuid.uuid4())[:8]
    session = ScanSession(sid, target)
    session.classifications = classifications
    session.entry_points = entry_points
    session.save()
    print(f"  Session saved: {sid}")
    print(f"  Run 'openhack --resume {sid}' to scan\n")


def _cmd_showkey():
    """Print the raw byte sequence your terminal sends for each keypress.

    Diagnostic for key-binding issues (e.g. Option+Backspace). It turns on the
    Kitty keyboard protocol AND xterm modifyOtherKeys, probes which the terminal
    supports, then prints the exact bytes each key produces so a binding can
    target them.
    """
    import os
    import select

    try:
        import termios
        import tty
    except ImportError:
        print("showkey needs a POSIX terminal.")
        return
    if not sys.stdin.isatty():
        print("Run this in a real terminal (stdin is not a TTY).")
        return

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)

    # Enable both disambiguation protocols so we see what the terminal emits
    # *with* them on. Kitty: CSI > 1 u (push). modifyOtherKeys: CSI > 4 ; 2 m.
    KITTY_QUERY = "\x1b[?u"
    KITTY_ON, KITTY_OFF = "\x1b[>1u", "\x1b[<u"
    MOK_ON, MOK_OFF = "\x1b[>4;2m", "\x1b[>4;0m"

    def _read_all(timeout):
        out = b""
        while select.select([fd], [], [], timeout)[0]:
            out += os.read(fd, 1)
            timeout = 0.02
        return out

    print("Key diagnostic — press keys to see the raw bytes (Ctrl-C to quit).", flush=True)
    print("Try: Option+Backspace, Ctrl-W, Option+Left.\n", flush=True)
    try:
        tty.setraw(fd)
        # Probe Kitty-protocol support: the terminal replies to CSI ? u only if
        # it implements the protocol.
        sys.stdout.write(KITTY_QUERY); sys.stdout.flush()
        reply = _read_all(0.3)
        kitty = reply.startswith(b"\x1b[?") and reply.endswith(b"u")
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        print(f"  Kitty keyboard protocol: {'SUPPORTED' if kitty else 'not supported'}"
              f"  (reply={reply!r})", flush=True)
        print("  Enabling Kitty + modifyOtherKeys for this session…\n", flush=True)
        tty.setraw(fd)
        sys.stdout.write(KITTY_ON + MOK_ON); sys.stdout.flush()

        while True:
            if not select.select([fd], [], [], None)[0]:
                continue
            buf = os.read(fd, 1)
            while select.select([fd], [], [], 0.02)[0]:
                buf += os.read(fd, 1)
            if buf in (b"\x03", b"\x04"):  # Ctrl-C / Ctrl-D
                break
            termios.tcsetattr(fd, termios.TCSADRAIN, old)  # restore to print
            hexs = " ".join(f"\\x{c:02x}" for c in buf)
            pretty = buf.decode("latin-1").replace("\x1b", "ESC")
            print(f"  {hexs:<24}  ({pretty!r})", flush=True)
            tty.setraw(fd)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            sys.stdout.write(KITTY_OFF + MOK_OFF); sys.stdout.flush()
        except Exception:
            pass
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    print(flush=True)


def _cmd_login():
    """Run the device login flow."""
    from openhack.setup import run_first_time_setup
    run_first_time_setup()


def _cmd_setup():
    """Run the setup wizard."""
    from openhack.setup import run_first_time_setup
    run_first_time_setup()


COMMANDS = {
    "hack": _cmd_hack,
    "plan": _cmd_plan,
    "agent": _cmd_agent,
    "interactive": _cmd_agent,
    "scan": _cmd_scan,
    "sessions": _cmd_sessions,
    "resume": _cmd_resume,
    "classify": _cmd_classify,
    "login": _cmd_login,
    "setup": _cmd_setup,
    "showkey": _cmd_showkey,
}


def _launch_tui(target=None, resume_session_id=None):
    """Launch the interactive TUI, optionally targeting a directory or resuming
    a saved session (its transcript restored, ready for a follow-up).

    Everything in the TUI reads os.getcwd(), so pointing it at a path is just a
    chdir before startup (the same thing the in-app /cd command does).
    """
    import os
    from pathlib import Path

    if target is not None:
        p = Path(target).expanduser()
        if not p.is_dir():
            print(f"Error: '{target}' is not a directory.")
            print("Usage: openhack [path]   (launches the TUI; path defaults to .)")
            return
        os.chdir(p.resolve())

    from openhack.setup import needs_first_time_setup, run_first_time_setup
    try:
        if needs_first_time_setup():
            completed = run_first_time_setup()
            if not completed:
                print("\nSetup skipped. Run 'openhack' again to retry.\n")
                return

        from openhack.tui import main as tui_main
        tui_main(resume_session_id=resume_session_id)
    except KeyboardInterrupt:
        print()


def main():
    if len(sys.argv) > 1:
        first = sys.argv[1]

        if first in ("--help", "-h", "help"):
            print(__doc__)
            return

        if first in ("--version", "-v", "version"):
            from openhack import __version__
            print(f"openhack {__version__}")
            return

        # Preferred form is a --flag (e.g. `openhack --scan`); the bare-word
        # form (`openhack scan`) still works for back-compat. A flag/subcommand
        # sits in argv[1] exactly where the old subcommand did, so the _cmd_*
        # handlers read their positional args unchanged.
        name = first[2:] if first.startswith("--") else first
        if name in COMMANDS:
            # A bare word that happens to name an existing directory is a path
            # for the TUI, not a subcommand (so `openhack scan/` opens ./scan).
            from pathlib import Path
            if not first.startswith("--") and Path(first).expanduser().is_dir():
                _launch_tui(first)
                return
            COMMANDS[name]()
            return

        if first.startswith("-"):
            print(f"Unknown option: {first}")
            print("Run 'openhack --help' for usage.")
            return

        # A bare argument that isn't a subcommand is treated as a path: open the
        # TUI targeting that directory.
        _launch_tui(first)
        return

    # No args: launch the TUI on the current directory.
    _launch_tui()


if __name__ == "__main__":
    main()
