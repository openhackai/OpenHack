"""
Session management for vulnerability scanning.
"""

import os
import signal
import threading
import time
from typing import Any, Optional
from uuid import uuid4
from enum import Enum
from dataclasses import dataclass, field


class SessionStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"


@dataclass
class Finding:
    """Represents a single vulnerability finding."""
    category: str
    severity: str
    title: str
    description: str
    file_path: str
    id: str = field(default_factory=lambda: str(uuid4()))
    line_number: Optional[int] = None
    code_snippet: Optional[str] = None
    poc: Optional[str] = None
    fix: Optional[str] = None
    cvss_score: Optional[float] = None
    confidence: str = "medium"
    validated: bool = False
    source: Optional[str] = None

    def fingerprint(self) -> str:
        import hashlib
        file_norm = (self.file_path or "").strip().lower().split(":")[0]
        cat_norm = self.category.strip().lower()
        raw = f"{cat_norm}::{file_norm}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "category": self.category,
            "severity": self.severity,
            "title": self.title,
            "description": self.description,
            "filePath": self.file_path,
            "lineNumber": self.line_number,
            "relevantCode": self.code_snippet,
            "poc": self.poc,
            "recommendation": self.fix,
            "cvssScore": self.cvss_score,
            "confidence": self.confidence,
            "validated": self.validated,
            "vulnerabilityType": self._generate_vulnerability_type(),
            "fingerprint": self.fingerprint(),
        }
        if self.source:
            d["verificationSource"] = self.source
        return d

    def _generate_vulnerability_type(self) -> str:
        category_lower = self.category.lower().replace(" ", "_").replace("-", "_")
        if "xss" in category_lower:
            if "dangerouslysetinnerhtml" in self.description.lower():
                return "xss_dangerously_set_html"
            elif "innerhtml" in self.description.lower():
                return "xss_innerhtml"
            elif "document.write" in self.description.lower():
                return "xss_document_write"
            return f"xss_{category_lower}"
        elif "sql" in category_lower or "injection" in category_lower:
            if "raw" in self.description.lower():
                return "sql_injection_raw_query"
            return "sql_injection"
        elif "idor" in category_lower:
            return "idor_direct_object_reference"
        elif "ssrf" in category_lower:
            return "ssrf_server_side_request"
        elif "csrf" in category_lower:
            return "csrf_missing_token"
        elif "auth" in category_lower:
            return "auth_bypass"
        return category_lower


@dataclass
class TraceEntry:
    """A single trace entry for debugging/logging."""
    timestamp: float
    agent: str
    event_type: str
    content: Any
    tool_name: Optional[str] = None
    tool_input: Optional[dict] = None
    tool_output: Optional[Any] = None


class Session:
    """Session to track scan state (in-memory)."""

    def __init__(
        self,
        target_dir: str,
        scan_id: Optional[str] = None,
        project_context: Optional[dict] = None,
        trace_id: Optional[str] = None,
        on_trace: Optional[Any] = None,
    ):
        self.id = scan_id or str(uuid4())
        self.trace_id = trace_id or (self.id[:8] if self.id else str(uuid4())[:8])
        self.target_dir = target_dir
        self.project_context = project_context
        self.status = SessionStatus.RUNNING
        self.created_at = time.time()
        self.updated_at = time.time()
        self.current_agent: Optional[str] = None
        self.current_step: Optional[str] = None
        self.findings: list[Finding] = []
        self.trace: list[TraceEntry] = []
        self.context: dict = {}
        self.total_cost: float = 0.0
        self.total_tokens: int = 0
        self.total_input_tokens: int = 0
        self.total_output_tokens: int = 0
        self.step_costs: dict[str, float] = {}
        self.step_tokens: dict[str, int] = {}
        self.step_input_tokens: dict[str, int] = {}
        self.step_output_tokens: dict[str, int] = {}
        self._on_trace = on_trace
        self._user_instructions: list[str] = []
        self._instructions_lock = threading.Lock()
        self._instructions_version: int = 0
        self.cancelled: bool = False
        # Subprocesses currently running on behalf of this session (spawned via
        # tools/process.run_killable). Tracked so cancel()/interrupt can signal-
        # kill them immediately instead of waiting for the worker thread that's
        # blocked on them. Guarded because tools register from worker threads
        # while cancel() fires from the event-loop thread.
        self._active_procs: set = set()
        self._proc_lock = threading.Lock()
        # Pause control: an asyncio.Event that's *set* when running (default)
        # and *cleared* when paused. Agents call `await wait_if_paused()`
        # between iterations, which blocks while the event is cleared.
        # Lazily created on first access because Session may be instantiated
        # outside an event loop (e.g. in serialization tests).
        self._pause_event: Optional[Any] = None

    def _ensure_pause_event(self) -> Any:
        if self._pause_event is None:
            import asyncio
            self._pause_event = asyncio.Event()
            self._pause_event.set()  # default: not paused
        return self._pause_event

    @property
    def paused(self) -> bool:
        return self._pause_event is not None and not self._pause_event.is_set()

    def pause(self) -> None:
        """Block agent loops at their next safe checkpoint."""
        self._ensure_pause_event().clear()

    def resume(self) -> None:
        """Unblock paused agent loops."""
        self._ensure_pause_event().set()

    async def wait_if_paused(self) -> None:
        """If the session is paused, await until resumed. No-op when not paused."""
        await self._ensure_pause_event().wait()

    def register_process(self, proc) -> None:
        """Track a live subprocess so cancel()/interrupt can kill it."""
        with self._proc_lock:
            self._active_procs.add(proc)

    def unregister_process(self, proc) -> None:
        with self._proc_lock:
            self._active_procs.discard(proc)

    def kill_active_processes(self) -> None:
        """Terminate every subprocess running for this session — SIGTERM now,
        SIGKILL any survivors ~0.4s later. This is what makes an interrupt stop a
        long-running tool at once: killing the child unblocks the worker thread
        waiting on it, so the agent loop breaks at its next checkpoint instead of
        after the command finishes on its own.
        """
        with self._proc_lock:
            procs = list(self._active_procs)
        survivors: list[tuple[Any, int]] = []
        for p in procs:
            try:
                if p.poll() is not None:
                    continue  # already exited & reaped — its PID may be recycled
                pgid = os.getpgid(p.pid)
                os.killpg(pgid, signal.SIGTERM)
                survivors.append((p, pgid))
            except (ProcessLookupError, PermissionError, OSError):
                pass  # already gone / not ours
        if survivors:
            def _sigkill() -> None:
                for p, pgid in survivors:
                    try:
                        if p.poll() is None:  # still alive → force it
                            os.killpg(pgid, signal.SIGKILL)
                    except OSError:
                        pass
            t = threading.Timer(0.4, _sigkill)
            t.daemon = True
            t.start()

    def cancel(self) -> None:
        """Set the cancellation flag so all agents break out of their loops.

        Also resumes the pause event so paused agents wake up and see the
        cancellation flag instead of blocking forever, and kills any subprocess
        a tool is currently blocked on so the stop is immediate.
        """
        self.cancelled = True
        if self._pause_event is not None:
            self._pause_event.set()
        self.kill_active_processes()

    def add_user_instruction(self, text: str) -> None:
        """Thread-safe: queue an instruction from the user during a running scan."""
        with self._instructions_lock:
            self._user_instructions.append(text)
            self._instructions_version += 1
        self.add_trace(agent="user", event_type="user_instruction", content=text)

    def get_new_instructions(self, seen_version: int) -> tuple[list[str], int]:
        """Thread-safe: return instructions added since *seen_version*.

        Unlike drain, this does NOT clear instructions — every agent that calls
        this will independently see every instruction added after its own
        watermark, which is critical for the swarm pattern where many agents
        run concurrently.

        Returns (new_instructions, current_version).
        """
        with self._instructions_lock:
            current = self._instructions_version
            if seen_version >= current:
                return [], current
            new = self._user_instructions[seen_version:]
            return list(new), current

    def get_all_instructions(self) -> list[str]:
        """Thread-safe: return a snapshot of all accumulated instructions."""
        with self._instructions_lock:
            return list(self._user_instructions)

    def add_trace(
        self,
        agent: str,
        event_type: str,
        content: Any,
        tool_name: Optional[str] = None,
        tool_input: Optional[dict] = None,
        tool_output: Optional[Any] = None,
    ) -> TraceEntry:
        entry = TraceEntry(
            timestamp=time.time(),
            agent=agent,
            event_type=event_type,
            content=content,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_output=tool_output,
        )
        self.trace.append(entry)
        self.updated_at = time.time()
        if self._on_trace:
            self._on_trace(entry)
        return entry

    def add_finding(self, finding: Finding) -> None:
        self.findings.append(finding)
        self.updated_at = time.time()

    def get_findings_dict(self) -> list[dict]:
        return [f.to_dict() for f in self.findings]

    def record_step_cost(
        self,
        step_name: str,
        cost: float,
        tokens: int,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        self.step_costs[step_name] = cost
        self.step_tokens[step_name] = tokens
        self.step_input_tokens[step_name] = input_tokens
        self.step_output_tokens[step_name] = output_tokens
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        self.updated_at = time.time()

    def restore_from_checkpoint(self, checkpoint_data: dict) -> None:
        """Restore session state from checkpoint data."""
        self.total_cost = checkpoint_data.get("total_cost", 0.0)
        self.total_tokens = checkpoint_data.get("total_tokens", 0)
        self.total_input_tokens = checkpoint_data.get("total_input_tokens", 0)
        self.total_output_tokens = checkpoint_data.get("total_output_tokens", 0)
        for step, cost in checkpoint_data.get("step_costs", {}).items():
            self.step_costs[step] = cost
        for step, tokens in checkpoint_data.get("step_tokens", {}).items():
            self.step_tokens[step] = tokens
        for step, tokens in checkpoint_data.get("step_input_tokens", {}).items():
            self.step_input_tokens[step] = tokens
        for step, tokens in checkpoint_data.get("step_output_tokens", {}).items():
            self.step_output_tokens[step] = tokens

    def get_cost_breakdown(self) -> dict:
        return {
            "total_cost": self.total_cost,
            "total_tokens": self.total_tokens,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "steps": {
                step: {
                    "cost": self.step_costs.get(step, 0),
                    "tokens": self.step_tokens.get(step, 0),
                    "input_tokens": self.step_input_tokens.get(step, 0),
                    "output_tokens": self.step_output_tokens.get(step, 0),
                }
                for step in self.step_costs
            }
        }
