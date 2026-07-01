"""
Headless-browser tool for DAST / dynamic verification.

Confirming a web vulnerability often needs a real browser: to render a reflected
XSS and see it execute, to follow a client-side redirect, to read the DOM after
JS runs, or to capture a screenshot as evidence. This tool drives headless
Chromium (Playwright) for exactly that — navigate, run JS in the page, read the
rendered DOM, and screenshot.

It is an **async** tool: the registry awaits it on the agent's event loop rather
than blocking. If Playwright isn't installed, it returns a clear, non-fatal
install hint instead of raising.
"""

from pathlib import Path
from typing import Optional


class BrowserTools:
    """Drive headless Chromium to verify web vulnerabilities dynamically."""

    is_async = True

    def __init__(self, evidence_dir: Optional[Path] = None):
        self.evidence_dir = Path(evidence_dir) if evidence_dir else (Path.cwd() / ".openhack-evidence")

    def _unavailable(self) -> Optional[dict]:
        try:
            import playwright.async_api  # noqa: F401
        except ImportError:
            return {
                "error": "browser_unavailable",
                "reason": (
                    "Playwright is not installed, so dynamic browser verification "
                    "isn't available. Install it: `pip install playwright && "
                    "playwright install chromium`."
                ),
            }
        return None

    async def browser_fetch(
        self,
        url: str,
        js: Optional[str] = None,
        screenshot: bool = False,
        wait_ms: int = 0,
        timeout: int = 30000,
    ) -> dict:
        """Load a URL in headless Chromium and return the rendered result.

        Optionally evaluate `js` in the page (its return value is captured),
        wait `wait_ms` after load, and save a screenshot. Use this to confirm
        client-side behaviour a raw HTTP fetch can't show (XSS execution,
        JS redirects, DOM state, dialogs).
        """
        if not url:
            return {"error": "missing_url"}
        unavailable = self._unavailable()
        if unavailable:
            return unavailable

        from playwright.async_api import async_playwright

        dialogs: list[str] = []
        console: list[str] = []
        result: dict = {"url": url}
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                try:
                    context = await browser.new_context(ignore_https_errors=True)
                    page = await context.new_page()
                    page.on("dialog", lambda d: (dialogs.append(f"{d.type}: {d.message}"),
                                                 _ignore(d.dismiss())))
                    page.on("console", lambda m: console.append(f"{m.type}: {m.text}"[:300]))

                    response = await page.goto(url, timeout=timeout, wait_until="load")
                    if wait_ms:
                        await page.wait_for_timeout(min(int(wait_ms), 15000))

                    result["status"] = response.status if response else None
                    result["final_url"] = page.url
                    result["title"] = await page.title()

                    if js:
                        try:
                            result["js_result"] = await page.evaluate(js)
                        except Exception as e:
                            result["js_error"] = str(e)[:300]

                    content = await page.content()
                    result["dom_excerpt"] = content[:8000]
                    result["dom_length"] = len(content)
                    if dialogs:
                        result["dialogs"] = dialogs  # e.g. alert() from an XSS payload
                    if console:
                        result["console"] = console[:50]

                    if screenshot:
                        self.evidence_dir.mkdir(parents=True, exist_ok=True)
                        import hashlib
                        fname = "shot_" + hashlib.sha1(url.encode()).hexdigest()[:10] + ".png"
                        shot_path = self.evidence_dir / fname
                        await page.screenshot(path=str(shot_path), full_page=True)
                        result["screenshot"] = str(shot_path)
                finally:
                    await browser.close()
        except Exception as e:
            return {"error": "browser_error", "detail": str(e)[:400], "url": url}
        return result

    # -------------------------------------------------------------- tool specs

    def get_tool_definitions(self) -> list[dict]:
        return [
            {
                "name": "browser_fetch",
                "description": (
                    "Load a URL in a real headless browser and return the rendered DOM, "
                    "final URL, status, title, any JS dialogs (e.g. an alert() from an "
                    "XSS payload) and console output. Optionally run JS in the page and "
                    "capture a screenshot. Use to dynamically verify web vulns that a "
                    "raw HTTP request can't confirm (reflected/DOM XSS, JS redirects, "
                    "client-side auth state)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "The URL to load (include the payload in the query if testing)."},
                        "js": {"type": "string", "description": "Optional JS to evaluate in the page; its return value is captured."},
                        "screenshot": {"type": "boolean", "description": "Save a full-page screenshot as evidence."},
                        "wait_ms": {"type": "integer", "description": "Milliseconds to wait after load (for async behaviour), max 15000."},
                    },
                    "required": ["url"],
                },
            },
        ]

    async def execute_tool_async(self, name: str, arguments: dict) -> dict:
        import inspect

        tools = {"browser_fetch": self.browser_fetch}
        if name not in tools:
            return {"error": f"Unknown tool: {name}"}
        func = tools[name]
        valid = set(inspect.signature(func).parameters.keys())
        filtered = {k: v for k, v in arguments.items() if k in valid}
        return await func(**filtered)

    def execute_tool(self, name: str, arguments: dict) -> dict:
        # Browser tools are async; the registry should route them through
        # execute_tool_async. This is only hit if called synchronously.
        return {"error": "browser_fetch is async; call via execute_tool_async"}


def _ignore(_coro):
    """Fire-and-forget a coroutine from a sync event handler."""
    import asyncio
    try:
        asyncio.ensure_future(_coro)
    except Exception:
        pass
