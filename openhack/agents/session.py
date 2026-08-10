"""
Session management for vulnerability scanning.
"""

import os
import signal
import subprocess
import threading
import time
from typing import Any, Optional
from uuid import uuid4
from enum import Enum
from dataclasses import dataclass, field
from pathlib import Path
from contextvars import ContextVar

from .eventlog import EventJournal


class SessionStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"
    CANCELLED = "cancelled"


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
    event_id: Optional[str] = None
    sequence: Optional[int] = None
    turn_id: Optional[str] = None
    model_call_id: Optional[str] = None
    tool_call_id: Optional[str] = None
    metadata: dict = field(default_factory=dict)


class Session:
    """Session to track scan state (in-memory)."""

    def __init__(
        self,
        target_dir: str,
        scan_id: Optional[str] = None,
        project_context: Optional[dict] = None,
        trace_id: Optional[str] = None,
        on_trace: Optional[Any] = None,
        on_event: Optional[Any] = None,
        event_log_path: Optional[str | Path] = None,
        persist_events: bool = True,
        parent_session_id: Optional[str] = None,
    ):
        self.id = scan_id or str(uuid4())
        self.trace_id = trace_id or (self.id[:8] if self.id else str(uuid4())[:8])
        self.parent_session_id = parent_session_id
        self.target_dir = str(Path(target_dir).resolve())
        self.project_context = project_context
        self.created_at = time.time()
        self.updated_at = time.time()
        self._status = SessionStatus.RUNNING
        self.status_history: list[dict[str, Any]] = []
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
        if event_log_path is None and persist_events:
            event_log_path = (
                Path.home() / ".openhack" / "scans" / f"{self.id}.events.jsonl"
            )
        self.journal = EventJournal(
            self.id,
            Path(event_log_path) if event_log_path else None,
            on_event=on_event,
        )
        self.event_log_path = str(self.journal.path) if self.journal.path else None
        # A scan swarm runs multiple agents concurrently. ContextVar keeps each
        # coroutine's correlation ID independent instead of letting agents
        # overwrite one shared ``current_turn_id``.
        self._turn_context: ContextVar[Optional[str]] = ContextVar(
            f"openhack_turn_{self.id}", default=None
        )
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
        self.record_event(
            "session_started",
            {
                "trace_id": self.trace_id,
                "parent_session_id": self.parent_session_id,
                "target_dir": self.target_dir,
                "pid": os.getpid(),
                "execution": self._execution_snapshot(),
                "repository": self._repository_snapshot(),
            },
            agent="system",
        )
        self._record_status(None, self._status, "session_created")

    @property
    def status(self) -> SessionStatus:
        return self._status

    @status.setter
    def status(self, value: SessionStatus | str) -> None:
        new_status = value if isinstance(value, SessionStatus) else SessionStatus(value)
        old_status = getattr(self, "_status", None)
        if old_status == new_status:
            return
        self._status = new_status
        if hasattr(self, "journal"):
            self._record_status(old_status, new_status, "assignment")

    def transition_status(self, value: SessionStatus | str, reason: str) -> None:
        new_status = value if isinstance(value, SessionStatus) else SessionStatus(value)
        old_status = self._status
        if old_status == new_status:
            return
        self._status = new_status
        self._record_status(old_status, new_status, reason)

    def _record_status(
        self,
        old_status: Optional[SessionStatus],
        new_status: SessionStatus,
        reason: str,
    ) -> None:
        transition = {
            "from": old_status.value if old_status else None,
            "to": new_status.value,
            "reason": reason,
            "timestamp": time.time(),
        }
        self.status_history.append(transition)
        self.record_event("session_status_changed", transition, agent="system")

    def _execution_snapshot(self) -> dict[str, Any]:
        return {
            "cwd": os.getcwd(),
            "filesystem_access": "unrestricted",
            "network_access": "enabled",
            "python": os.sys.version.split()[0],
        }

    def _repository_snapshot(self) -> dict[str, Any]:
        def git(*args: str) -> str:
            try:
                return subprocess.run(
                    ["git", "-C", self.target_dir, *args],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    check=False,
                ).stdout.strip()
            except Exception:
                return ""

        worktrees: list[dict[str, str]] = []
        current: dict[str, str] = {}
        for line in git("worktree", "list", "--porcelain").splitlines():
            if not line:
                if current:
                    worktrees.append(current)
                    current = {}
                continue
            key, _, value = line.partition(" ")
            current[key] = value
        if current:
            worktrees.append(current)
        return {
            "root": git("rev-parse", "--show-toplevel"),
            "head": git("rev-parse", "HEAD"),
            "branch": git("branch", "--show-current"),
            "status": git("status", "--short"),
            "worktrees": worktrees,
        }

    @property
    def events(self) -> list:
        return self.journal.records

    @property
    def current_turn_id(self) -> Optional[str]:
        return self._turn_context.get()

    @current_turn_id.setter
    def current_turn_id(self, value: Optional[str]) -> None:
        self._turn_context.set(value)

    def record_event(
        self,
        event_type: str,
        data: Any = None,
        *,
        agent: str = "",
        turn_id: Optional[str] = None,
        model_call_id: Optional[str] = None,
        tool_call_id: Optional[str] = None,
        parent_event_id: Optional[str] = None,
        durable: bool = True,
    ):
        self.updated_at = time.time()
        return self.journal.append(
            event_type,
            data,
            agent=agent,
            turn_id=turn_id or self.current_turn_id,
            model_call_id=model_call_id,
            tool_call_id=tool_call_id,
            parent_event_id=parent_event_id,
            durable=durable,
        )

    def start_turn(self, task: str, agent: str) -> str:
        self.current_turn_id = str(uuid4())
        self.record_event(
            "turn_started",
            {"task": task},
            agent=agent,
            turn_id=self.current_turn_id,
        )
        return self.current_turn_id

    def finish_turn(self, outcome: str, data: Any = None, *, agent: str = "") -> None:
        self.record_event(
            "turn_finished",
            {"outcome": outcome, "result": data},
            agent=agent,
            turn_id=self.current_turn_id,
        )
        self.current_turn_id = None

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
        self.transition_status(SessionStatus.PAUSED, "user_pause")

    def resume(self) -> None:
        """Unblock paused agent loops."""
        self._ensure_pause_event().set()
        self.cancelled = False
        self.transition_status(SessionStatus.RUNNING, "user_resume")

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
        from openhack.tools.process import kill_process_group

        with self._proc_lock:
            procs = list(self._active_procs)
        survivors: list[Any] = []
        for p in procs:
            try:
                if p.poll() is not None:
                    continue  # already exited & reaped — its PID may be recycled
                kill_process_group(p, signal.SIGTERM)
                survivors.append(p)
            except (ProcessLookupError, PermissionError, OSError):
                pass  # already gone / not ours
        if survivors:
            def _sigkill() -> None:
                for p in survivors:
                    try:
                        if p.poll() is None:  # still alive → force it
                            kill_process_group(p)
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
        self.transition_status(SessionStatus.CANCELLED, "user_interrupt")
        self.record_event(
            "session_cancel_requested",
            {"active_processes": len(self._active_procs)},
            agent="user",
        )
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
        turn_id: Optional[str] = None,
        model_call_id: Optional[str] = None,
        tool_call_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> TraceEntry:
        event = self.record_event(
            f"trace.{event_type}",
            {
                "content": content,
                "tool_name": tool_name,
                "tool_input": tool_input,
                "tool_output": tool_output,
                "metadata": metadata or {},
            },
            agent=agent,
            turn_id=turn_id,
            model_call_id=model_call_id,
            tool_call_id=tool_call_id,
        )
        entry = TraceEntry(
            timestamp=time.time(),
            agent=agent,
            event_type=event_type,
            content=content,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_output=tool_output,
            event_id=event.event_id,
            sequence=event.sequence,
            turn_id=turn_id or self.current_turn_id,
            model_call_id=model_call_id,
            tool_call_id=tool_call_id,
            metadata=metadata or {},
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
