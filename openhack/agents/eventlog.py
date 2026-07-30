"""Durable, append-only operational event journal.

The human-facing trace is intentionally compact.  The journal is the forensic
record used to explain *why* an agent stopped and to recover partial work after
an interrupt, provider failure, or process crash.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional
from uuid import uuid4


SCHEMA_VERSION = 1
_SECRET_KEY = re.compile(
    r"(authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|password|"
    r"passwd|cookie|private[_-]?key|client[_-]?secret)",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=-]+")
_KNOWN_TOKEN = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,}|"
    r"AKIA[0-9A-Z]{16})\b"
)


def redact(value: Any, key: str = "") -> Any:
    """Return a JSON-safe value with common credentials removed."""
    if key and _SECRET_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [redact(v) for v in value]
    if isinstance(value, bytes):
        return {
            "_type": "bytes",
            "size": len(value),
            "sha256": hashlib.sha256(value).hexdigest(),
        }
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        return _KNOWN_TOKEN.sub("[REDACTED]", _BEARER.sub(r"\1[REDACTED]", value))
    if value is None or isinstance(value, (bool, int, float)):
        return value
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return {"_type": type(value).__name__, "repr": str(value)}


@dataclass
class EventRecord:
    sequence: int
    session_id: str
    event_type: str
    timestamp: float = field(default_factory=time.time)
    monotonic_ns: int = field(default_factory=time.monotonic_ns)
    event_id: str = field(default_factory=lambda: str(uuid4()))
    agent: str = ""
    turn_id: Optional[str] = None
    model_call_id: Optional[str] = None
    tool_call_id: Optional[str] = None
    parent_event_id: Optional[str] = None
    data: Any = None
    previous_hash: str = ""
    event_hash: str = ""
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EventJournal:
    """Thread-safe JSONL journal with sequence numbers and a hash chain."""

    def __init__(
        self,
        session_id: str,
        path: Optional[Path] = None,
        on_event: Optional[Callable[[EventRecord], None]] = None,
        *,
        fsync: bool = True,
    ):
        self.session_id = session_id
        self.path = Path(path).expanduser() if path else None
        self.on_event = on_event
        self.fsync = fsync
        self.records: list[EventRecord] = []
        self._sequence = 0
        self._previous_hash = ""
        self._lock = threading.Lock()
        self.last_error: Optional[str] = None
        if self.path and self.path.exists():
            tail = self._last_valid_record()
            if tail:
                self._sequence = int(tail.get("sequence") or 0)
                self._previous_hash = str(tail["event_hash"])

    def _last_valid_record(self) -> Optional[dict[str, Any]]:
        """Read only the tail of a potentially very large journal."""
        if self.path is None:
            return None
        try:
            with open(self.path, "rb") as fp:
                fp.seek(0, os.SEEK_END)
                end = fp.tell()
                block = 8192
                data = b""
                position = end
                while position > 0:
                    take = min(block, position)
                    position -= take
                    fp.seek(position)
                    data = fp.read(take) + data
                    lines = [line for line in data.splitlines() if line.strip()]
                    for line in reversed(lines):
                        try:
                            parsed = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(parsed, dict) and parsed.get("event_hash"):
                            return parsed
                    if len(lines) >= 2:
                        break
        except OSError:
            return None
        return None

    def append(
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
    ) -> EventRecord:
        with self._lock:
            self._sequence += 1
            record = EventRecord(
                sequence=self._sequence,
                session_id=self.session_id,
                event_type=event_type,
                agent=agent,
                turn_id=turn_id,
                model_call_id=model_call_id,
                tool_call_id=tool_call_id,
                parent_event_id=parent_event_id,
                data=redact(data),
                previous_hash=self._previous_hash,
            )
            unsigned = record.to_dict()
            unsigned["event_hash"] = ""
            encoded = json.dumps(
                unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
            record.event_hash = hashlib.sha256(encoded).hexdigest()
            self._previous_hash = record.event_hash
            self.records.append(record)
            self._persist(record, durable=durable)
        if self.on_event:
            try:
                self.on_event(record)
            except Exception:
                pass
        return record

    def _persist(self, record: EventRecord, *, durable: bool) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(self.path.parent, 0o700)
            except OSError:
                pass
            payload = json.dumps(
                record.to_dict(), default=str, ensure_ascii=False, separators=(",", ":")
            )
            with open(self.path, "a", encoding="utf-8") as fp:
                try:
                    os.chmod(self.path, 0o600)
                except OSError:
                    pass
                fp.write(payload + "\n")
                fp.flush()
                if self.fsync and durable:
                    os.fsync(fp.fileno())
            self.last_error = None
        except Exception as exc:
            # Persistence failures remain visible in memory.  Never pretend a
            # durable write succeeded, but do not recursively journal the error.
            self.last_error = f"{type(exc).__name__}: {exc}"

    def load(self) -> list[dict[str, Any]]:
        if self.path is None or not self.path.exists():
            return []
        loaded: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                loaded.append(json.loads(line))
            except json.JSONDecodeError:
                loaded.append({"event_type": "journal_corruption", "raw": line})
        return loaded

    def verify(self) -> dict[str, Any]:
        """Verify sequence continuity, previous hashes, and every event hash."""
        previous_hash = ""
        expected_sequence = 1
        errors: list[dict[str, Any]] = []
        records = self.load()
        valid_count = 0
        for record in records:
            if "event_hash" not in record:
                errors.append({"sequence": expected_sequence, "error": "invalid_json"})
                continue
            sequence = int(record.get("sequence") or 0)
            if sequence != expected_sequence:
                errors.append({
                    "sequence": sequence,
                    "error": "sequence_gap",
                    "expected": expected_sequence,
                })
            if record.get("previous_hash", "") != previous_hash:
                errors.append({"sequence": sequence, "error": "previous_hash_mismatch"})
            claimed = str(record.get("event_hash") or "")
            unsigned = dict(record)
            unsigned["event_hash"] = ""
            encoded = json.dumps(
                unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
            actual = hashlib.sha256(encoded).hexdigest()
            if actual != claimed:
                errors.append({"sequence": sequence, "error": "event_hash_mismatch"})
            else:
                valid_count += 1
            previous_hash = claimed
            expected_sequence = sequence + 1
        return {
            "valid": not errors,
            "records": len(records),
            "valid_records": valid_count,
            "errors": errors,
        }
