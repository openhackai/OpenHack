"""Web research tools: search backend selection/parsing and HTML→text fetch."""

import json

import pytest

from openhack.tools.registry import ToolRegistry
from openhack.tools.web import WebTools, extract_text


class _Resp:
    def __init__(self, text="", status=200, headers=None, url="https://x/", payload=None):
        self.text = text if payload is None else json.dumps(payload)
        self.status_code = status
        self.headers = headers or {"content-type": "text/html"}
        self.url = url
        self._payload = payload

    def json(self):
        return self._payload if self._payload is not None else json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


# ------------------------------------------------------------------ extractor

def test_extract_text_strips_scripts_styles_and_keeps_title():
    title, text = extract_text(
        "<html><head><title>Advisory</title><style>.a{color:red}</style></head>"
        "<body><script>var x=1;</script><h1>CVE-2026-63030</h1>"
        "<p>Batch API route confusion.</p></body></html>"
    )
    assert title == "Advisory"
    assert "CVE-2026-63030" in text and "Batch API route confusion." in text
    assert "var x=1" not in text and "color:red" not in text


def test_extract_text_passes_through_non_html():
    _, text = extract_text("just some plain text")
    assert text == "just some plain text"


# --------------------------------------------------------------- provider pick

_KEYS = ("OPENHACK_SEARCH_PROVIDER", "TAVILY_API_KEY", "PERPLEXITY_API_KEY",
         "EXA_API_KEY", "BRAVE_API_KEY")


def test_provider_prefers_keys_then_falls_back(monkeypatch):
    for k in _KEYS:
        monkeypatch.delenv(k, raising=False)
    assert WebTools._provider() == "duckduckgo"      # keyless default
    monkeypatch.setenv("BRAVE_API_KEY", "b")
    assert WebTools._provider() == "brave"
    monkeypatch.setenv("EXA_API_KEY", "e")
    assert WebTools._provider() == "exa"
    monkeypatch.setenv("PERPLEXITY_API_KEY", "p")
    assert WebTools._provider() == "perplexity"
    monkeypatch.setenv("TAVILY_API_KEY", "t")
    assert WebTools._provider() == "tavily"          # highest priority
    monkeypatch.setenv("OPENHACK_SEARCH_PROVIDER", "exa")
    assert WebTools._provider() == "exa"             # explicit override wins


def test_web_search_perplexity(monkeypatch):
    # Perplexity's raw Search API (POST /search) — results carry extracted
    # snippets; no answer-model sits in front of them.
    for k in _KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PERPLEXITY_API_KEY", "k")
    w = WebTools()
    captured = {}

    def _post(url, payload, headers=None, **kw):
        captured.update(url=url, payload=payload, headers=headers or {})
        return _Resp(payload={"results": [{
            "title": "wp2shell writeup", "url": "https://slcyber.io/x",
            "snippet": "REST batch route confusion", "date": "2026-07-01",
        }]})

    monkeypatch.setattr(w, "_post", _post)
    out = w.web_search("wp2shell", max_results=5)
    assert captured["url"] == "https://api.perplexity.ai/search"
    assert captured["payload"]["max_results"] == 5
    assert captured["headers"]["Authorization"] == "Bearer k"
    assert out["engine"] == "perplexity"
    assert out["results"][0]["url"] == "https://slcyber.io/x"
    assert "route confusion" in out["results"][0]["snippet"]
    assert out["results"][0]["date"] == "2026-07-01"


# --------------------------------------------------------------------- search

def test_web_search_tavily(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "k")
    w = WebTools()
    monkeypatch.setattr(w, "_post", lambda *a, **k: _Resp(payload={
        "results": [{"title": "wp2shell", "url": "https://slcyber.io/x", "content": "pre-auth RCE"}]
    }))
    out = w.web_search("wp2shell rce")
    assert out["engine"] == "tavily" and out["count"] == 1
    assert out["results"][0]["url"] == "https://slcyber.io/x"
    assert "pre-auth RCE" in out["results"][0]["snippet"]


def test_web_search_duckduckgo_parses_and_unwraps_redirect(monkeypatch):
    for k in _KEYS:
        monkeypatch.delenv(k, raising=False)
    markup = (
        '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fslcyber.io%2Fpost">'
        "wp2shell writeup</a>"
        '<a class="result__snippet" href="#">Pre-auth RCE in WordPress core</a>'
    )
    w = WebTools()
    monkeypatch.setattr(w, "_get", lambda *a, **k: _Resp(markup))
    out = w.web_search("wp2shell")
    assert out["engine"] == "duckduckgo"
    assert out["results"][0]["url"] == "https://slcyber.io/post"
    assert out["results"][0]["title"] == "wp2shell writeup"


def test_web_search_missing_query():
    assert WebTools().web_search("")["error"] == "missing_query"


def test_web_search_failure_is_recoverable(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "k")
    w = WebTools()

    def _boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(w, "_post", _boom)
    out = w.web_search("q")
    assert out["error"] == "search_failed" and "network down" in out["reason"]


# ---------------------------------------------------------------------- fetch

def test_web_fetch_returns_readable_text(monkeypatch):
    w = WebTools()
    monkeypatch.setattr(w, "_get", lambda *a, **k: _Resp(
        "<html><head><title>Post</title></head><body><script>x</script>"
        "<p>Route confusion in serve_batch_request_v1.</p></body></html>",
        url="https://slcyber.io/post",
    ))
    out = w.web_fetch("slcyber.io/post")  # scheme auto-added
    assert out["status"] == 200 and out["title"] == "Post"
    assert "serve_batch_request_v1" in out["text"]
    assert "<script>" not in out["text"]


def test_web_fetch_truncates_with_note(monkeypatch):
    w = WebTools()
    monkeypatch.setattr(w, "_get", lambda *a, **k: _Resp("<p>" + "A" * 5000 + "</p>"))
    out = w.web_fetch("https://x/", max_chars=1000)
    assert out["truncated"] is True and len(out["text"]) == 1000
    assert "max_chars" in out["note"]


def test_web_fetch_flags_js_rendered_page(monkeypatch):
    w = WebTools()
    monkeypatch.setattr(w, "_get", lambda *a, **k: _Resp("<html><body><script>app()</script></body></html>"))
    out = w.web_fetch("https://x/")
    assert out["text"] == ""
    assert "browser_fetch" in out["note"]


def test_web_fetch_flags_bot_protection(monkeypatch):
    # Cloudflare-style interstitial (what slcyber.io actually returns) must be
    # reported as blocked with a "try another source" hint, not "JS-rendered".
    w = WebTools()
    monkeypatch.setattr(w, "_get", lambda *a, **k: _Resp(
        "<html><head><title>Just a moment...</title></head><body></body></html>", status=403
    ))
    out = w.web_fetch("https://slcyber.io/x")
    assert out["blocked"] is True
    assert "another source" in out["note"] and "403" in out["note"]


def test_web_fetch_error_is_recoverable(monkeypatch):
    w = WebTools()

    def _boom(*a, **k):
        raise RuntimeError("dns fail")

    monkeypatch.setattr(w, "_get", _boom)
    out = w.web_fetch("https://nope/")
    assert out["error"] == "fetch_failed" and "dns fail" in out["reason"]


def test_web_fetch_missing_url():
    assert WebTools().web_fetch("")["error"] == "missing_url"


# -------------------------------------------------------------------- wiring

def test_tools_registered_for_agents_only(tmp_path):
    agent_names = {
        t["name"] for t in
        ToolRegistry(target_dir=tmp_path, include_agent_tools=True).get_all_tool_definitions()
    }
    assert {"web_search", "web_fetch"} <= agent_names
    scan_names = {
        t["name"] for t in
        ToolRegistry(target_dir=tmp_path).get_all_tool_definitions()
    }
    assert "web_search" not in scan_names  # scan pipeline toolset unchanged


def test_registry_dispatches_web_fetch(tmp_path, monkeypatch):
    reg = ToolRegistry(target_dir=tmp_path, include_agent_tools=True)
    monkeypatch.setattr(reg.web_tools, "_get", lambda *a, **k: _Resp("<p>hello</p>"))
    out = reg.execute_tool("web_fetch", {"url": "https://x/"})
    assert "hello" in out["text"]


def test_plan_mode_allows_research_tools():
    from openhack.agents.interactive import _PLAN_ALLOWED_TOOLS
    assert {"web_search", "web_fetch"} <= _PLAN_ALLOWED_TOOLS
