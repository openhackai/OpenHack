"""Stall watchdog + tool-arg streaming, from session cfeb868f.

That run opened a request at 01:08:58 and sat on it for 274s — over half the
total runtime — until the user hit Esc. No retry fired and no timeout fired,
because the socket kept receiving bytes the whole time: the upstream wedged but
its SSE keepalive comments kept arriving, and httpx resets its read clock on
every byte. The openai SDK's decoder discards comment lines, so the client saw
zero events while believing the connection was healthy.

A byte-level timeout structurally cannot catch that. These tests pin the
progress-level one that can.
"""

import asyncio
import http.server
import json
import socketserver
import threading
import time

import openai
import pytest

from openhack.agents.llm import LLMClient, StreamStalled


# --------------------------------------------------------------- fake upstream

class _StreamHandler(http.server.BaseHTTPRequestHandler):
    """Serves one SSE response whose behaviour is set by MODE."""

    MODE = "keepalive"

    def log_message(self, *a):  # keep pytest output clean
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length", 0)))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def send(obj):
            self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode())
            self.wfile.flush()

        def chunk(delta):
            return {"id": "1", "object": "chat.completion.chunk", "created": 0,
                    "model": "m", "choices": [{"index": 0, "delta": delta,
                                               "finish_reason": None}]}

        try:
            if self.MODE == "keepalive":
                # 200, one real token, then nothing but keepalives forever.
                send(chunk({"role": "assistant", "content": "hi"}))
                for _ in range(120):
                    time.sleep(0.25)
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
            elif self.MODE == "tool_args":
                send(chunk({"tool_calls": [{"index": 0, "id": "c1", "type": "function",
                                            "function": {"name": "write_file",
                                                         "arguments": ""}}]}))
                for part in ('{"path":', '"a.py",', '"content":', '"xxxx"}'):
                    send(chunk({"tool_calls": [{"index": 0,
                                                "function": {"arguments": part}}]}))
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            elif self.MODE == "slow_but_alive":
                # Real tokens, slower than the stall window between them would
                # be if we measured total time rather than idle time.
                for i in range(6):
                    time.sleep(0.3)
                    send(chunk({"content": f"tok{i}"}))
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


class _Server:
    def __init__(self, mode):
        _StreamHandler.MODE = mode
        socketserver.TCPServer.allow_reuse_address = True
        self.srv = socketserver.TCPServer(("127.0.0.1", 0), _StreamHandler)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.srv.shutdown()
        self.srv.server_close()


def _client(port, stall, monkeypatch, retries=0):
    from openhack import config as _config
    import openhack.agents.llm as _llm_mod
    # The production floor exists to survive prefill on a big context; tests
    # need a short one, so lower it explicitly rather than sleeping through it.
    monkeypatch.setattr(_llm_mod, "MIN_STALL_TIMEOUT", 1)
    monkeypatch.setattr(_config.settings, "openhack_stream_stall_timeout", stall)
    monkeypatch.setattr(_config.settings, "openhack_max_retries", retries)
    import openhack.agents.llm as _llm
    monkeypatch.setattr(_llm.settings, "openhack_max_retries", retries)

    c = LLMClient.__new__(LLMClient)
    c.model = "m"
    c.max_tokens = 64
    c.temperature = 0.0
    c.prompt_cache_key = None
    c._resolved = None
    c.status_callback = None
    c.total_cost = 0.0
    c.total_tokens = 0
    c.total_input_tokens = 0
    c.total_output_tokens = 0
    c.PRICING = {}
    c.client = openai.AsyncOpenAI(api_key="k", base_url=f"http://127.0.0.1:{port}/v1",
                                  timeout=30.0, max_retries=0)
    return c


# ------------------------------------------------------------------- the bug

def test_keepalive_only_stream_is_abandoned_not_waited_on(monkeypatch):
    """The cfeb868f hang. The socket is healthy; the generation is not."""
    from openhack.agents.llm import Message

    with _Server("keepalive") as s:
        c = _client(s.port, stall=3, monkeypatch=monkeypatch)
        t0 = time.monotonic()
        with pytest.raises(StreamStalled):
            asyncio.run(c._chat([Message(role="user", content="go")], None, "sys"))
        waited = time.monotonic() - t0

    # Must give up on its own, near the stall limit — not hang until the caller
    # or the user kills it.
    assert waited < 10, f"took {waited:.1f}s to notice a dead generation"
    assert waited >= 3


def test_a_bare_read_timeout_would_not_have_caught_it():
    """Why the existing 120s read timeout was never going to fire.

    Same server, no watchdog — just httpx with a short read timeout. It never
    trips, because keepalive bytes keep arriving.
    """
    import httpx

    with _Server("keepalive") as s:
        t0 = time.monotonic()
        got_timeout = False
        try:
            with httpx.Client(timeout=1.0) as h:
                with h.stream("POST", f"http://127.0.0.1:{s.port}/v1/chat/completions",
                              json={"model": "m"}) as r:
                    for _ in r.iter_bytes():
                        if time.monotonic() - t0 > 4:
                            break          # still alive well past the timeout
        except httpx.ReadTimeout:
            got_timeout = True

    assert not got_timeout, "keepalives no longer defeat the read timeout"


def test_slow_but_progressing_stream_is_not_killed(monkeypatch):
    """The watchdog measures idle time, not elapsed time — a long, steadily
    streaming answer must survive past the stall limit."""
    from openhack.agents.llm import Message

    with _Server("slow_but_alive") as s:
        c = _client(s.port, stall=1, monkeypatch=monkeypatch)
        r = asyncio.run(c._chat([Message(role="user", content="go")], None, "sys"))

    # ~1.8s total with 0.3s between tokens, against a 1s stall limit.
    assert r.content == "tok0tok1tok2tok3tok4tok5"


def test_stalled_stream_is_retried(monkeypatch):
    """A stall is transient, so it goes through the retry ladder — which also
    gives the gateway another chance to fail over to a different provider."""
    from openhack.agents.llm import Message

    seen = []
    with _Server("keepalive") as s:
        c = _client(s.port, stall=2, monkeypatch=monkeypatch, retries=1)
        c.status_callback = seen.append
        with pytest.raises(StreamStalled):
            asyncio.run(c._chat([Message(role="user", content="go")], None, "sys"))

    assert any("quiet" in m or "reconnect" in m for m in seen), seen


# ------------------------------------------------- tool-arg stream visibility

def test_tool_call_arguments_reach_the_ui(monkeypatch):
    """Writing a 20KB file is minutes of pure argument stream. Before this the
    deltas were accumulated silently and the transcript showed nothing at all."""
    from openhack.agents.llm import Message

    seen: list[tuple[str, str]] = []
    with _Server("tool_args") as s:
        c = _client(s.port, stall=10, monkeypatch=monkeypatch)
        r = asyncio.run(c._chat([Message(role="user", content="go")], None, "sys",
                                on_chunk=lambda k, d: seen.append((k, d))))

    kinds = [k for k, _ in seen]
    assert "tool_args" in kinds, "argument deltas never surfaced"
    assert "".join(d for k, d in seen if k == "tool_args") == \
        '{"path":"a.py","content":"xxxx"}'
    # ...and the call still parses correctly.
    assert r.tool_calls[0].name == "write_file"
    assert r.tool_calls[0].arguments["path"] == "a.py"


def test_tui_shows_progress_while_tool_args_stream():
    from openhack.tui import OpenHackApp

    app = OpenHackApp.__new__(OpenHackApp)
    app._stream_buf = ""
    app._stream_reasoning = "some earlier thinking"
    app._stream_tool_bytes = 0
    app._stream_last_invalidate = 0.0
    app._interrupting = False
    app._shell_active = False
    app._spin_idx = 0
    app._invalidate = lambda: None

    app._on_agent_stream("tool_args", "x" * 2048)
    assert app._stream_tool_bytes == 2048

    text = "".join(t for _, t in app._stream_line())
    assert "2.0 KB" in text
    # The frozen thinking tail must not be what's on screen — that's what made
    # a live write look stalled.
    assert "some earlier thinking" not in text
    assert app._processing_verb() == "writing"


def test_unknown_stream_kinds_are_ignored():
    from openhack.tui import OpenHackApp

    app = OpenHackApp.__new__(OpenHackApp)
    app._stream_buf = ""
    app._stream_reasoning = ""
    app._stream_tool_bytes = 0
    app._stream_last_invalidate = 0.0
    app._invalidate = lambda: None

    app._on_agent_stream("something_new", "data")
    assert app._stream_buf == "" and app._stream_tool_bytes == 0


# ------------------------------------------------------------ token accounting

def test_session_accumulates_input_and_output_tokens():
    """Reports showed total_tokens: 692110 next to total_input_tokens: 0."""
    from openhack.agents.session import Session

    s = Session(target_dir=".")
    usage = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}

    s.total_tokens += usage["total_tokens"]
    s.total_input_tokens += usage["input_tokens"]
    s.total_output_tokens += usage["output_tokens"]

    assert (s.total_input_tokens, s.total_output_tokens) == (100, 20)
    assert s.total_input_tokens + s.total_output_tokens == s.total_tokens


def test_base_agent_records_both_token_directions():
    """Pins the real call site, not just the arithmetic."""
    import inspect
    from openhack.agents import base

    src = inspect.getsource(base.BaseAgent)
    assert "total_input_tokens += response.usage" in src
    assert "total_output_tokens += response.usage" in src
