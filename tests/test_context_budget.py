"""Context budget + stall guards, from session 265af3d8.

That run took 6.3 min to do very little. Two causes:
  * a 159.7s dead gap ending in an APIError — a hung request tolerated for up
    to the 600s read timeout, then an uncapped 5→10→20→40→80s backoff ladder;
  * tool results flooding the context: one web_search returned 50,000+ chars
    (8 results × 6k of page body), ~263k chars across 14 results, all of it
    re-sent on every later turn.
"""

import json

import pytest

from openhack.model_catalog import bundled_models
from openhack.agents.context_manager import ContextWindowManager, MODEL_CONTEXT_LIMITS
from openhack.tools.web import (
    _CONTENT_RESULTS,
    _DEFAULT_RESULTS,
    _MAX_CONTENT,
    _MAX_TEXT,
    WebTools,
)


def _cm():
    return ContextWindowManager(
        context_window_limit=200_000, compaction_threshold=0.8, tool_result_max_lines=200
    )


def test_latest_hosted_models_use_their_real_context_tiers():
    assert MODEL_CONTEXT_LIMITS["grok-4.6"] == 500_000
    assert MODEL_CONTEXT_LIMITS["glm-5.3"] == 1_048_576
    assert MODEL_CONTEXT_LIMITS["kimi-k3"] == 1_000_000
    assert MODEL_CONTEXT_LIMITS["deepseek-v4-pro"] == 1_000_000
    assert MODEL_CONTEXT_LIMITS["deepseek-v4-flash-0731"] == 1_000_000
    assert MODEL_CONTEXT_LIMITS["minimax-m3"] == 524_288
    assert MODEL_CONTEXT_LIMITS["hy3"] == 262_144
    assert MODEL_CONTEXT_LIMITS["step-3.7-flash"] == 262_144
    assert MODEL_CONTEXT_LIMITS["mimo-v2.5-pro"] == 1_048_576


def test_every_openhack_family_model_has_an_explicit_context_limit():
    ids = {
        model["id"] for model in bundled_models("openhack")
        if model["tab"] == "openhack"
    }
    assert ids - MODEL_CONTEXT_LIMITS.keys() == set()


# ------------------------------------------------------------ truncation

def test_oversized_tool_result_stays_valid_json():
    # The old path sliced the serialized blob at 8k, handing the model a broken
    # object cut mid-value.
    payload = {
        "engine": "openhack", "query": "wp2shell", "count": 3,
        "results": [
            {"title": f"r{i}", "url": f"https://x/{i}", "content": "C" * 9000}
            for i in range(3)
        ],
    }
    out = _cm().truncate_tool_result("web_search", json.dumps(payload))
    parsed = json.loads(out)                       # must not raise
    assert len(parsed["results"]) == 3             # every result survives
    assert parsed["engine"] == "openhack"          # scalar keys intact
    assert len(out) <= 10_000


def test_truncation_leaves_small_results_alone():
    small = json.dumps({"status": 200, "text": "short"})
    assert _cm().truncate_tool_result("web_fetch", small) == small


def test_truncation_trims_the_longest_field_first():
    payload = {"tiny": "x" * 50, "huge": "y" * 30_000}
    parsed = json.loads(_cm().truncate_tool_result("anything", json.dumps(payload)))
    assert parsed["tiny"] == "x" * 50              # short field untouched
    assert len(parsed["huge"]) < 30_000            # long one cut


def test_error_results_are_never_truncated():
    err = json.dumps({"error": "boom", "detail": "d" * 20_000})
    assert _cm().truncate_tool_result("web_fetch", err) == err


# --------------------------------------------------------- web tool sizing

def test_search_result_payload_is_bounded():
    # A single search must not be able to dominate the context.
    assert _CONTENT_RESULTS * _MAX_CONTENT <= 10_000, "search payload budget blown"
    assert _MAX_TEXT <= 12_000
    assert _DEFAULT_RESULTS <= 6


def test_only_the_top_results_carry_a_page_body(monkeypatch):
    for k in ("OPENHACK_SEARCH_PROVIDER", "PERPLEXITY_API_KEY", "EXA_API_KEY", "BRAVE_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "k")
    w = WebTools()

    class _R:
        status_code = 200
        headers = {"content-type": "application/json"}
        url = "https://x/"

        def json(self):
            return {"results": [
                {"title": f"t{i}", "url": f"https://x/{i}",
                 "content": "teaser", "raw_content": "B" * 5000}
                for i in range(8)
            ]}

        def raise_for_status(self):
            pass

    monkeypatch.setattr(w, "_post", lambda *a, **k: _R())
    out = w.web_search("q", max_results=8)
    with_body = [r for r in out["results"] if r.get("content")]
    assert len(with_body) == _CONTENT_RESULTS
    for r in with_body:
        assert len(r["content"]) <= _MAX_CONTENT
    total = sum(len(json.dumps(r)) for r in out["results"])
    assert total < 12_000, f"search payload too large: {total}"


# ------------------------------------------------------------ stall guards

def test_retry_backoff_is_capped():
    from openhack.agents.llm import MAX_RETRY_BACKOFF
    from openhack.config import settings
    waits = [min(5 * (2 ** (i - 1)), MAX_RETRY_BACKOFF)
             for i in range(1, settings.openhack_max_retries + 1)]
    assert max(waits) <= 20
    assert sum(waits) <= 90, "backoff ladder is most of a visible stall"


def test_read_timeout_catches_a_hang_promptly():
    # Per-socket-read, and every call streams — so this bounds SILENCE, not the
    # length of a generation. 600s meant a dead upstream froze the UI for ten
    # minutes before it even errored.
    from openhack.config import settings
    assert settings.openhack_read_timeout <= 180
