"""
Disposable-mailbox tools for the agent.

Wraps the OpenHack `inbox` CLI so the agent can complete email-gated flows on
its own — signup confirmations, OTP codes, password resets, admin invites —
which are otherwise dead ends for an automated pentest. Mint a fresh address
per flow, trigger the email, then block on `mailbox_wait` until it arrives and
the verification code / magic link is surfaced.

Auth and endpoint come from the environment the operator already set up
(`INBOX_TOKEN`, `INBOX_URL`, `INBOX_DOMAIN`). If the CLI isn't installed or no
token is configured, the tools return a clear, non-fatal explanation.
"""

import json
import os
import subprocess
from shutil import which
from typing import Optional

from openhack.tools.process import run_killable


class MailboxTools:
    """Mint disposable inboxes and wait for verification mail."""

    WAIT_HARD_CAP = 600  # seconds

    def __init__(self, session=None):
        # When set, the `inbox` CLI (esp. the blocking mailbox_wait poll)
        # registers with the session so ESC/cancel can kill it immediately.
        self._session = session

    def _available(self) -> Optional[dict]:
        """Return an error dict if the mailbox isn't usable, else None."""
        if which("inbox") is None:
            return {
                "error": "mailbox_unavailable",
                "reason": (
                    "The `inbox` CLI is not installed, so disposable email isn't "
                    "available. Install openhack-mailbox's CLI to enable email-gated "
                    "flows (signup codes, OTPs, resets)."
                ),
            }
        if not (os.environ.get("INBOX_TOKEN") or os.environ.get("INBOX_URL")):
            return {
                "error": "mailbox_unconfigured",
                "reason": (
                    "No mailbox credentials found. Set INBOX_TOKEN (and optionally "
                    "INBOX_URL / INBOX_DOMAIN) to use disposable email."
                ),
            }
        return None

    def _run(self, args: list[str], timeout: int) -> subprocess.CompletedProcess:
        return run_killable(
            ["inbox", *args],
            session=self._session, timeout=timeout,
            env=os.environ.copy(),
        )

    # ------------------------------------------------------------------ tools

    def mailbox_new(self, label: Optional[str] = None) -> dict:
        """Mint a fresh disposable email address for a flow."""
        unavailable = self._available()
        if unavailable:
            return unavailable

        args = ["new", "--json"]
        if label:
            # Keep the label shell-safe; the CLI slugs it anyway.
            args = ["new", "--label", str(label), "--json"]
        try:
            proc = self._run(args, timeout=30)
        except subprocess.TimeoutExpired:
            return {"error": "timeout", "reason": "`inbox new` timed out"}

        if proc.returncode != 0:
            return {"error": "inbox_new_failed", "detail": (proc.stderr or proc.stdout)[:500]}
        return self._parse_json(proc.stdout, fallback_key="address")

    def mailbox_wait(
        self,
        to: str,
        match: Optional[str] = None,
        timeout: int = 180,
    ) -> dict:
        """Block until a matching email arrives, surfacing codes and links.

        `to` is the address from mailbox_new. `match` is an optional regex to
        filter on (e.g. 'code|verify|confirm|reset'). Start this *before* or right
        as you trigger the email — it only matches mail that arrives after the wait
        begins.
        """
        unavailable = self._available()
        if unavailable:
            return unavailable
        if not to:
            return {"error": "missing_address", "reason": "`to` (the address to watch) is required"}

        wait_timeout = min(int(timeout) if timeout else 180, self.WAIT_HARD_CAP)
        args = ["wait", "--to", to, "--timeout", str(wait_timeout), "--json"]
        if match:
            args += ["--match", match]

        try:
            # Give the subprocess a little longer than its own deadline.
            proc = self._run(args, timeout=wait_timeout + 20)
        except subprocess.TimeoutExpired:
            return {"error": "timeout", "timed_out": True,
                    "reason": f"No matching mail within {wait_timeout}s"}

        if proc.returncode == 2:
            return {"timed_out": True, "reason": f"No matching mail arrived within {wait_timeout}s"}
        if proc.returncode != 0:
            return {"error": "inbox_wait_failed", "exit_code": proc.returncode,
                    "detail": (proc.stderr or proc.stdout)[:500]}
        return self._parse_json(proc.stdout, fallback_key="message")

    def mailbox_list(self, to: Optional[str] = None, match: Optional[str] = None, limit: int = 20) -> dict:
        """List recent messages (newest first) for triage."""
        unavailable = self._available()
        if unavailable:
            return unavailable
        args = ["list", "--json", "--limit", str(int(limit) if limit else 20)]
        if to:
            args += ["--to", to]
        if match:
            args += ["--match", match]
        try:
            proc = self._run(args, timeout=30)
        except subprocess.TimeoutExpired:
            return {"error": "timeout", "reason": "`inbox list` timed out"}
        if proc.returncode != 0:
            return {"error": "inbox_list_failed", "detail": (proc.stderr or proc.stdout)[:500]}
        return self._parse_json(proc.stdout, fallback_key="messages")

    # ----------------------------------------------------------------- helpers

    def _parse_json(self, out: str, fallback_key: str) -> dict:
        out = (out or "").strip()
        if not out:
            return {fallback_key: None}
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return {fallback_key: out[:2000]}
        return data if isinstance(data, dict) else {fallback_key: data}

    # -------------------------------------------------------------- tool specs

    def get_tool_definitions(self) -> list[dict]:
        return [
            {
                "name": "mailbox_new",
                "description": (
                    "Mint a fresh disposable email address (e.g. "
                    "signup-1a2b3c4d@inbox.openhack.com) to use in a signup, OTP or "
                    "password-reset flow. Returns the address to submit to the target."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "label": {
                            "type": "string",
                            "description": "Optional label prefix for the address (e.g. 'signup').",
                        },
                    },
                },
            },
            {
                "name": "mailbox_wait",
                "description": (
                    "Block until an email arrives at a disposable address, then surface "
                    "any verification code and priority links (confirm/verify/reset/"
                    "magic-login). Start this right as you trigger the email. Use after "
                    "mailbox_new to complete email-gated flows."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "to": {
                            "type": "string",
                            "description": "The address to watch (from mailbox_new).",
                        },
                        "match": {
                            "type": "string",
                            "description": "Optional regex to filter mail, e.g. 'code|verify|confirm|reset'.",
                        },
                        "timeout": {
                            "type": "integer",
                            "description": "Seconds to wait before giving up (default 180, max 600).",
                        },
                    },
                    "required": ["to"],
                },
            },
            {
                "name": "mailbox_list",
                "description": "List recent messages for a disposable address (newest first) to triage what arrived.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string", "description": "Filter to this address."},
                        "match": {"type": "string", "description": "Optional regex filter."},
                        "limit": {"type": "integer", "description": "Max messages to return (default 20)."},
                    },
                },
            },
        ]

    def execute_tool(self, name: str, arguments: dict) -> dict:
        import inspect

        tools = {
            "mailbox_new": self.mailbox_new,
            "mailbox_wait": self.mailbox_wait,
            "mailbox_list": self.mailbox_list,
        }
        if name not in tools:
            return {"error": f"Unknown tool: {name}"}
        func = tools[name]
        valid = set(inspect.signature(func).parameters.keys())
        filtered = {k: v for k, v in arguments.items() if k in valid}
        return func(**filtered)
