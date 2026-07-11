"""
Stateful headless-browser tool source for specialist exploiters.

The registry's default `browser_fetch` (tools/browser.py) is one-shot: it spins up
a fresh browser per call, so it can't hold a session across navigate → fill → click
— exactly what a real XSS victim-bot flow (log in, submit a payload, observe the
admin/bot rendering it) needs. That richer, *stateful* capability already exists
inside `BrowserVerifierAgent`, but only as a bespoke inline loop wired to the
white-box verifier.

This class exposes that same capability as a normal async tool source so any agent
(specifically the XSS specialist) can drive a persistent browser through the
standard agent loop. It REUSES the existing `BrowserRunner` (openhack/browser/runner.py)
and the existing tool specs from `browser_verifier.BROWSER_TOOLS` — nothing is
reimplemented or removed. `base_url=""` means absolute http(s) URLs pass straight
through, so it works for black-box targets (operator URL) as well as the sandbox.
"""

from pathlib import Path
from typing import Optional

from openhack.browser.runner import BrowserRunner, BrowserContext


# Runner method for each browser_* tool name (mirrors BrowserVerifierAgent._handle_*).
_ARG_MAP = {
    "browser_navigate": ("navigate", lambda a: {"url": a.get("url", "/"), "wait_until": a.get("wait_until", "networkidle")}),
    "browser_click": ("click", lambda a: {"selector": a.get("selector", ""), "selector_type": a.get("selector_type", "css")}),
    "browser_fill": ("fill", lambda a: {"selector": a.get("selector", ""), "value": a.get("value", "")}),
    "browser_screenshot": ("screenshot", lambda a: {"name": a.get("name", "screenshot")}),
    "browser_get_content": ("get_content", lambda a: {"selector": a.get("selector"), "fmt": a.get("format", "text")}),
    "browser_execute_js": ("execute_js", lambda a: {"script": a.get("script", "")}),
    "browser_wait_for": ("wait_for", lambda a: {"selector": a.get("selector", ""), "timeout": a.get("timeout_ms", 5000), "state": a.get("state", "visible")}),
    "browser_get_cookies": ("get_cookies", lambda a: {}),
    "browser_snapshot": ("snapshot", lambda a: {}),
}


class StatefulBrowserTools:
    """Drive a persistent Playwright session (navigate/fill/click/js/snapshot…)."""

    is_async = True

    def __init__(self, base_url: str = "", evidence_dir: Optional[Path] = None):
        # base_url="" → absolute URLs pass through (runner.py navigate()); a set
        # base_url lets the white-box swarm point specialists at the live sandbox.
        self.base_url = base_url
        self.evidence_dir = Path(evidence_dir) if evidence_dir else (Path.cwd() / ".openhack-evidence")
        self._runner: Optional[BrowserRunner] = None
        self._ctx: Optional[BrowserContext] = None

    @staticmethod
    def _unavailable() -> Optional[dict]:
        try:
            import playwright.async_api  # noqa: F401
        except ImportError:
            return {
                "error": "browser_unavailable",
                "reason": (
                    "Playwright is not installed, so the stateful browser isn't "
                    "available. Install it: `pip install playwright && "
                    "playwright install chromium`."
                ),
            }
        return None

    def get_tool_definitions(self) -> list[dict]:
        # Reuse the existing browser specs (lazy import avoids any import cycle:
        # this runs at registry-build time, when openhack.agents is fully loaded).
        from openhack.agents.browser_verifier import BROWSER_TOOLS
        return [t for t in BROWSER_TOOLS if t["name"] != "report_browser_result"]

    async def _ensure(self) -> Optional[dict]:
        """Lazily launch the browser + a single persistent context on first use."""
        if self._ctx is not None:
            return None
        unavailable = self._unavailable()
        if unavailable is not None:
            return unavailable
        try:
            self._runner = BrowserRunner(base_url=self.base_url, evidence_dir=self.evidence_dir)
            await self._runner.__aenter__()
            self._ctx = await self._runner.create_context(0)
        except Exception as e:
            await self.aclose()
            return {"error": "browser_launch_failed", "reason": str(e)}
        return None

    async def execute_tool_async(self, name: str, arguments: dict) -> dict:
        if name not in _ARG_MAP:
            return {"error": f"Unknown tool: {name}"}
        err = await self._ensure()
        if err is not None:
            return err
        method_name, arg_fn = _ARG_MAP[name]
        method = getattr(self._runner, method_name)
        try:
            result = await method(self._ctx, **arg_fn(arguments))
        except Exception as e:
            return {"error": f"{name} failed", "reason": str(e)}
        return result.to_dict() if hasattr(result, "to_dict") else result

    def execute_tool(self, name: str, arguments: dict) -> dict:
        return {"error": f"{name} is async; call via execute_tool_async"}

    async def aclose(self) -> None:
        """Tear down the context/runner. Safe to call multiple times."""
        try:
            if self._runner is not None:
                await self._runner.__aexit__(None, None, None)
        except Exception:
            pass
        finally:
            self._runner = None
            self._ctx = None
