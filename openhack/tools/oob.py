"""
Out-of-band (OOB) interaction tools for blind vulnerability verification.

Many high-impact bugs are *blind*: blind SSRF, blind RCE, blind XXE, blind SQLi
with an exfil channel, DNS/HTTP callbacks from a deserialization gadget. You
can't see the result in the HTTP response — you confirm it by making the target
call back to a server you control.

These tools wrap the OpenHack OOB collector (openhack-oob):
  * oob_register — mint a unique callback URL to embed in a payload
  * oob_poll     — check whether the target has called back to that URL yet

The marker embedded in the URL path is what correlates a callback to a specific
payload, so the agent can fire several probes and tell which one fired.
"""

import json
import os
import secrets
import urllib.parse
import urllib.request
from typing import Optional


class OOBTools:
    """Register OOB callback URLs and poll for interactions."""

    def _base_url(self) -> str:
        return os.environ.get("OOB_URL", "https://oob.openhack.com").rstrip("/")

    def _token(self) -> Optional[str]:
        return os.environ.get("OOB_TOKEN")

    def oob_register(self, label: Optional[str] = None) -> dict:
        """Mint a unique OOB callback URL to embed in a blind payload.

        Returns HTTP and (bare-host) forms plus the `marker` used to poll. No
        server round-trip is needed to register — any request to the URL is
        recorded automatically; poll with the marker to see callbacks.
        """
        marker = (self._slug(label) + "-" if label else "") + secrets.token_hex(6)
        base = self._base_url()
        host = urllib.parse.urlparse(base).netloc or base
        return {
            "marker": marker,
            "http_url": f"{base}/{marker}",
            "callback_host": host,
            "callback_path_url": f"{base}/{marker}",
            "note": (
                "Embed one of these in your payload (e.g. an SSRF URL, an XXE "
                "SYSTEM entity, an RCE `curl`/`nslookup` to the host). Then call "
                "oob_poll with the marker to see if the target called back. "
                "Requires OOB_TOKEN to poll."
            ),
        }

    def oob_poll(self, marker: str, since_ms: int = 0, limit: int = 100) -> dict:
        """Check whether any callback matching `marker` has been recorded."""
        if not marker:
            return {"error": "missing_marker"}
        token = self._token()
        if not token:
            return {
                "error": "oob_unconfigured",
                "reason": "Set OOB_TOKEN to read OOB callbacks from the collector.",
            }
        params = {
            "token": token,
            "q": marker,
            "since": int(since_ms) if since_ms else 0,
            "limit": min(int(limit) if limit else 100, 500),
        }
        url = f"{self._base_url()}/_hits?" + urllib.parse.urlencode(params)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "openhack"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception as e:
            return {"error": "oob_poll_failed", "detail": str(e)[:300]}

        hits = data.get("hits", []) if isinstance(data, dict) else []
        # Trim each hit to the fields that matter for verification.
        trimmed = [
            {
                "ts": h.get("ts"),
                "method": h.get("method"),
                "path": h.get("path"),
                "query": h.get("query"),
                "ip": h.get("ip"),
                "host": h.get("host"),
            }
            for h in hits[:100]
        ]
        return {
            "marker": marker,
            "interactions": len(trimmed),
            "fired": len(trimmed) > 0,
            "hits": trimmed,
        }

    def _slug(self, label: str) -> str:
        return "".join(c for c in label.lower() if c.isalnum() or c == "-")[:24] or "oob"

    # -------------------------------------------------------------- tool specs

    def get_tool_definitions(self) -> list[dict]:
        return [
            {
                "name": "oob_register",
                "description": (
                    "Mint a unique out-of-band callback URL to embed in a blind-vuln "
                    "payload (blind SSRF/RCE/XXE, exfil). Returns the URL and a marker; "
                    "poll the marker with oob_poll to confirm the target called back."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Optional label to prefix the marker (e.g. 'ssrf')."},
                    },
                },
            },
            {
                "name": "oob_poll",
                "description": (
                    "Check whether the target has made an out-of-band callback to a "
                    "marker from oob_register — i.e. whether a blind vulnerability "
                    "fired. Returns the recorded interactions."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "marker": {"type": "string", "description": "The marker returned by oob_register."},
                        "since_ms": {"type": "integer", "description": "Only interactions after this epoch-ms (default 0)."},
                        "limit": {"type": "integer", "description": "Max interactions to return (default 100)."},
                    },
                    "required": ["marker"],
                },
            },
        ]

    def execute_tool(self, name: str, arguments: dict) -> dict:
        import inspect

        tools = {
            "oob_register": self.oob_register,
            "oob_poll": self.oob_poll,
        }
        if name not in tools:
            return {"error": f"Unknown tool: {name}"}
        func = tools[name]
        valid = set(inspect.signature(func).parameters.keys())
        filtered = {k: v for k, v in arguments.items() if k in valid}
        return func(**filtered)
