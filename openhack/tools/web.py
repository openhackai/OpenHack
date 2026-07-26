"""Web research tools: search the web, and fetch a page as readable text.

Without these the agent can only guess URLs and scrape them with
`curl | python3 -c 're.sub…'`, which is exactly what it resorts to when asked
about a fresh CVE. `web_search` gives it ranked results (advisories, PoC repos,
vendor writeups); `web_fetch` turns a page into clean text so it doesn't have to
hand-roll HTML stripping.

Search is provider-agnostic and key-driven, mirroring `providers.py`: whichever
of Tavily / Exa / Brave has a key in the environment is used, otherwise it falls
back to a keyless DuckDuckGo query so the tool always works with zero setup.

`web_fetch` uses plain HTTP (httpx) plus a stdlib HTML→text extractor — no
Playwright required. For pages that genuinely need JS execution, the agent still
has `browser_fetch` from BrowserTools.
"""

from __future__ import annotations

import html
import json
import os
import re
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import parse_qs, quote_plus, urlparse

__all__ = ["WebTools", "extract_text"]


class _Throttled(RuntimeError):
    """Keyless search backend rate-limited us — distinct from 'no results'."""

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

_MAX_TEXT = 20_000    # chars of extracted page text handed back to the model
_MAX_SNIPPET = 400    # short teaser, for scanning the result list
_MAX_CONTENT = 6_000  # extracted page body per result, when the backend gives one


def _result(title, url, snippet="", content="", **extra) -> dict:
    """Normalize one search hit. `content` (the extracted page body, when the
    backend provides it) is the valuable part — it often carries pages that
    can't be fetched directly because of bot protection."""
    out = {
        "title": (title or "").strip(),
        "url": url or "",
        "snippet": " ".join(str(snippet or "").split())[:_MAX_SNIPPET],
    }
    body = (content or "").strip()
    if len(body) > len(out["snippet"]):
        out["content"] = body[:_MAX_CONTENT]
        if len(body) > _MAX_CONTENT:
            out["content_truncated"] = True
    out.update({k: v for k, v in extra.items() if v})
    return out

# Tags whose contents are never readable page text. (Not <head> — <title> lives
# there and we want it.)
_SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "iframe"}
# Tags that imply a line break when they close.
_BLOCK_TAGS = {
    "p", "div", "br", "li", "tr", "section", "article", "header", "footer",
    "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote", "ul", "ol", "table",
}


class _TextExtractor(HTMLParser):
    """Minimal readability: page title + visible text, scripts/styles dropped."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.saw_tag = False   # distinguishes real markup from plain text/JSON
        self._in_title = False
        self._skip_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.saw_tag = True
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag == "title":
            self._in_title = False
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data.strip()
            return
        if data.strip():
            self._parts.append(data)

    def text(self) -> str:
        raw = "".join(self._parts)
        raw = re.sub(r"[ \t\r\f\v]+", " ", raw)
        raw = re.sub(r" *\n *", "\n", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


def extract_text(markup: str) -> tuple[str, str]:
    """Return (title, readable_text) for an HTML document."""
    parser = _TextExtractor()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:
        pass  # tolerate malformed markup — keep whatever we parsed
    text = parser.text()
    if not text.strip() and not parser.saw_tag:
        # Not markup at all (JSON/plain text) — hand it back as-is. When we DID
        # see tags, empty text is meaningful: the page is JS-rendered.
        text = markup.strip()
    return parser.title.strip(), text


class WebTools:
    """Search the web and read pages as text."""

    SEARCH_TIMEOUT = 20
    FETCH_TIMEOUT = 30

    def __init__(self, timeout: Optional[int] = None):
        self._timeout = timeout

    # ------------------------------------------------------------------ http

    def _get(self, url: str, headers: Optional[dict] = None, timeout: Optional[int] = None):
        import httpx

        return httpx.get(
            url,
            headers={"User-Agent": _UA, **(headers or {})},
            timeout=timeout or self._timeout or self.FETCH_TIMEOUT,
            follow_redirects=True,
        )

    def _post(self, url: str, payload: dict, headers: Optional[dict] = None,
              timeout: Optional[int] = None):
        import httpx

        return httpx.post(
            url,
            json=payload,
            headers={"User-Agent": _UA, **(headers or {})},
            timeout=timeout or self._timeout or self.SEARCH_TIMEOUT,
            follow_redirects=True,
        )

    # ---------------------------------------------------------------- search

    @staticmethod
    def _provider() -> str:
        """Pick a search backend: an explicit override, else whichever key is set,
        else keyless DuckDuckGo."""
        forced = (os.environ.get("OPENHACK_SEARCH_PROVIDER") or "").strip().lower()
        if forced:
            return forced
        if os.environ.get("TAVILY_API_KEY"):
            return "tavily"
        if os.environ.get("PERPLEXITY_API_KEY"):
            return "perplexity"
        if os.environ.get("EXA_API_KEY"):
            return "exa"
        if os.environ.get("BRAVE_API_KEY"):
            return "brave"
        return "duckduckgo"

    def web_search(self, query: str, max_results: int = 8) -> dict:
        """Search the web and return ranked results (title, url, snippet)."""
        if not query or not query.strip():
            return {"error": "missing_query"}
        n = max(1, min(int(max_results or 8), 20))
        provider = self._provider()
        try:
            if provider == "tavily":
                results = self._search_tavily(query, n)
            elif provider == "perplexity":
                results = self._search_perplexity(query, n)
            elif provider == "exa":
                results = self._search_exa(query, n)
            elif provider == "brave":
                results = self._search_brave(query, n)
            else:
                provider = "duckduckgo"
                results = self._search_duckduckgo(query, n)
        except _Throttled as e:
            # Explicit: the agent must NOT read this as "nothing exists".
            return {
                "error": "search_throttled",
                "engine": provider,
                "reason": str(e),
                "note": (
                    "This is a rate limit, not an empty result set — do not conclude "
                    "the topic has no coverage. Retry once, or use web_fetch on a known "
                    "URL. For reliable search set one of TAVILY_API_KEY / "
                    "PERPLEXITY_API_KEY / EXA_API_KEY / BRAVE_API_KEY."
                ),
            }
        except Exception as e:
            return {
                "error": "search_failed",
                "engine": provider,
                "reason": str(e)[:300],
                "note": (
                    "Search failed. Set TAVILY_API_KEY (or PERPLEXITY_API_KEY / "
                    "EXA_API_KEY / BRAVE_API_KEY) for a reliable backend, or fetch a "
                    "known URL with web_fetch."
                ),
            }
        if not results:
            return {
                "engine": provider, "query": query, "count": 0, "results": [],
                "note": "No results. Try different keywords, or web_fetch a known URL.",
            }
        return {"engine": provider, "query": query, "count": len(results), "results": results}

    def _search_tavily(self, query: str, n: int) -> list[dict]:
        r = self._post(
            "https://api.tavily.com/search",
            {
                "api_key": os.environ["TAVILY_API_KEY"],
                "query": query,
                "max_results": n,
                "search_depth": "advanced",
                # Ask for the extracted page body, not just a teaser: this is
                # what lets the agent read a Cloudflare-gated page it could
                # never fetch directly.
                "include_raw_content": True,
            },
        )
        r.raise_for_status()
        out = []
        for it in (r.json().get("results") or [])[:n]:
            body = (it.get("raw_content") or it.get("content") or "").strip()
            out.append(_result(
                title=it.get("title"), url=it.get("url"),
                snippet=(it.get("content") or body), content=body,
            ))
        return out

    def _search_perplexity(self, query: str, n: int) -> list[dict]:
        """Perplexity's raw Search API (POST /search) — ranked results with
        extracted page content. Note this is NOT the answer/chat endpoint: no
        model sits in front of the results, so nothing summarizes or filters
        them. `search_context_size: high` maximizes the extracted snippet."""
        r = self._post(
            "https://api.perplexity.ai/search",
            {
                "query": query,
                "max_results": n,
                # Pull a real chunk of each page, not just a teaser.
                "max_tokens_per_page": 2048,
            },
            headers={"Authorization": f"Bearer {os.environ['PERPLEXITY_API_KEY']}"},
        )
        r.raise_for_status()
        return [
            _result(title=it.get("title"), url=it.get("url"),
                    snippet=it.get("snippet"), content=it.get("snippet"),
                    date=it.get("date"))
            for it in (r.json().get("results") or [])[:n]
        ]

    def _search_exa(self, query: str, n: int) -> list[dict]:
        r = self._post(
            "https://api.exa.ai/search",
            {
                "query": query, "numResults": n,
                "contents": {"text": {"maxCharacters": _MAX_CONTENT}},
            },
            headers={"x-api-key": os.environ["EXA_API_KEY"]},
        )
        r.raise_for_status()
        return [
            _result(title=it.get("title"), url=it.get("url"),
                    snippet=it.get("text") or it.get("snippet"),
                    content=it.get("text") or "")
            for it in (r.json().get("results") or [])[:n]
        ]

    def _search_brave(self, query: str, n: int) -> list[dict]:
        r = self._get(
            f"https://api.search.brave.com/res/v1/web/search?q={quote_plus(query)}&count={n}",
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": os.environ["BRAVE_API_KEY"],
            },
            timeout=self.SEARCH_TIMEOUT,
        )
        r.raise_for_status()
        web = (r.json().get("web") or {}).get("results") or []
        return [
            _result(title=it.get("title"), url=it.get("url"),
                    snippet=re.sub(r"<[^>]+>", "", it.get("description") or ""))
            for it in web[:n]
        ]

    def _search_duckduckgo(self, query: str, n: int) -> list[dict]:
        """Keyless fallback — scrape DuckDuckGo's no-JS endpoints.

        DDG rate-limits scraping aggressively and answers a throttled request
        with a 200 + an empty result page, which is indistinguishable from "no
        such thing exists" unless we look. So: try both no-JS endpoints with a
        short backoff, and raise _Throttled rather than silently returning
        nothing — a wrong "no results" is worse than an explicit failure.
        """
        import time as _time

        endpoints = (
            "https://html.duckduckgo.com/html/?q={q}",
            "https://lite.duckduckgo.com/lite/?q={q}",
        )
        last_markup = ""
        for attempt in range(3):
            url = endpoints[attempt % len(endpoints)].format(q=quote_plus(query))
            try:
                r = self._get(url, timeout=self.SEARCH_TIMEOUT)
                r.raise_for_status()
            except Exception:
                _time.sleep(0.6 * (attempt + 1))
                continue
            results = self._parse_ddg(r.text, n)
            if results:
                return results
            last_markup = r.text
            _time.sleep(0.6 * (attempt + 1))
        # Nothing parsed from any endpoint. A real empty result set is a short,
        # well-formed page; a throttle/challenge is not.
        if self._looks_throttled(last_markup):
            raise _Throttled(
                "DuckDuckGo throttled the request (keyless scraping is rate-limited)."
            )
        return []

    @staticmethod
    def _looks_throttled(markup: str) -> bool:
        low = (markup or "").lower()
        if not low:
            return True
        return any(s in low for s in (
            "anomaly", "unusual traffic", "captcha", "challenge",
            "blocked", "too many requests", "rate limit",
        )) or "result__a" not in low

    @staticmethod
    def _parse_ddg(markup: str, n: int) -> list[dict]:
        out: list[dict] = []
        seen: set[str] = set()
        # Result anchors carry class="result__a"; snippets class="result__snippet".
        anchors = re.findall(
            r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            markup, re.S | re.I,
        )
        snippets = re.findall(
            r'class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', markup, re.S | re.I
        )
        for i, (href, title_html) in enumerate(anchors):
            url = html.unescape(href)
            # DDG wraps targets as /l/?uddg=<encoded>
            if "uddg=" in url:
                qs = parse_qs(urlparse(url).query)
                url = (qs.get("uddg") or [url])[0]
            if not url.startswith("http") or url in seen:
                continue
            seen.add(url)
            title = html.unescape(re.sub(r"<[^>]+>", "", title_html)).strip()
            snippet = ""
            if i < len(snippets):
                snippet = html.unescape(re.sub(r"<[^>]+>", "", snippets[i])).strip()[:500]
            out.append({"title": title, "url": url, "snippet": snippet})
            if len(out) >= n:
                break
        return out

    # ----------------------------------------------------------------- fetch

    def web_fetch(self, url: str, max_chars: int = _MAX_TEXT) -> dict:
        """Fetch a URL and return its readable text (HTML stripped)."""
        if not url or not url.strip():
            return {"error": "missing_url"}
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        try:
            r = self._get(url, timeout=self.FETCH_TIMEOUT)
        except Exception as e:
            return {"error": "fetch_failed", "url": url, "reason": str(e)[:300]}

        ctype = (r.headers.get("content-type") or "").lower()
        body = r.text
        if "application/json" in ctype:
            title, text = "", body.strip()
        else:
            title, text = extract_text(body)

        cap = max(1000, min(int(max_chars or _MAX_TEXT), 100_000))
        truncated = len(text) > cap
        result = {
            "url": url,
            "final_url": str(r.url),
            "status": r.status_code,
            "title": title,
            "content_type": ctype.split(";")[0],
            "text": text[:cap],
        }
        if truncated:
            result["truncated"] = True
            result["note"] = (
                f"Showing the first {cap:,} of {len(text):,} chars. Raise max_chars, "
                "or fetch a more specific URL, for the rest."
            )
        blocked = r.status_code in (401, 403, 405, 406, 429) or r.status_code >= 500 or (
            "just a moment" in title.lower() or "attention required" in title.lower()
        )
        if blocked:
            result["blocked"] = True
            result["note"] = (
                f"Blocked by the site (HTTP {r.status_code}"
                + (f", '{title}'" if title else "")
                + ") — typically bot protection (Cloudflare), not a missing page. "
                "Read the same material from another source (run web_search and pick a "
                "different result), or try browser_fetch."
            )
        elif not text.strip():
            result["note"] = (
                "No readable text — the page is likely JS-rendered. Try browser_fetch "
                "(needs Playwright), or fetch the underlying API/raw URL instead."
            )
        return result

    # ------------------------------------------------------------ tool specs

    def get_tool_definitions(self) -> list[dict]:
        return [
            {
                "name": "web_search",
                "description": (
                    "Search the web and get ranked results (title, URL, snippet). Use "
                    "this to research anything you don't already know: a CVE's technical "
                    "details, a vendor advisory, a public PoC/exploit repo, a library's "
                    "known bugs, default credentials, or an unfamiliar technology. Follow "
                    "up with web_fetch on the promising URLs to read them in full. Prefer "
                    "this over guessing URLs."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The search query, e.g. 'CVE-2026-63030 WordPress REST batch RCE PoC'.",
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "How many results to return (default 8, max 20).",
                        },
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "web_fetch",
                "description": (
                    "Fetch a URL and return the page as clean, readable text (HTML/scripts "
                    "stripped, JSON passed through). Use this to actually read an advisory, "
                    "blog post, docs page, or raw file — don't hand-roll `curl | sed/regex`. "
                    "If a page returns no text it is JS-rendered; use browser_fetch instead."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "The URL to fetch."},
                        "max_chars": {
                            "type": "integer",
                            "description": "Max characters of text to return (default 20000).",
                        },
                    },
                    "required": ["url"],
                },
            },
        ]

    def execute_tool(self, name: str, arguments: dict) -> dict:
        import inspect

        tools = {"web_search": self.web_search, "web_fetch": self.web_fetch}
        if name not in tools:
            return {"error": f"Unknown tool: {name}"}
        func = tools[name]
        valid = set(inspect.signature(func).parameters.keys())
        return func(**{k: v for k, v in (arguments or {}).items() if k in valid})
