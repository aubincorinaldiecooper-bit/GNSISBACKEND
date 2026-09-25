"""One shared causal timeline per live session (AGENTS.md decision 10).

Every component reports ordered events into a single in-process plane:
mic/turn binds, model output epochs, delivery transitions, memory episodes,
playback ACKs, interruptions. The timeline is the coordination boundary that
keeps perception, output, tasks, and memory replaceable underneath — an
event-sourcing rewrite is deliberately avoided; this is an in-memory ordered
plane plus an optional structured JSONL log durable enough to reconstruct a
run.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any, Iterable

LOGGER = logging.getLogger(__name__)

MAX_TIMELINE_EVENTS = 4096


@dataclass(frozen=True)
class TimelineEvent:
    """One entry on the shared session timeline.

    ``seq`` is the monotonic order in this session; ``source_ts_ms`` is the
    originator's clock when available (client capture, model event) and
    ``recv_ts_ms`` the server receive timestamp; epochs/correlation ids tie
    the event to the call/output epoch it belongs to.
    """

    seq: int
    kind: str
    component: str
    session_id: str
    recv_ts_ms: int
    source_ts_ms: int | None
    call_epoch: int
    output_epoch: int
    correlation_id: str | None
    fields: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "kind": self.kind,
            "component": self.component,
            "session_id": self.session_id,
            "recv_ts_ms": self.recv_ts_ms,
            "source_ts_ms": self.source_ts_ms,
            "call_epoch": self.call_epoch,
            "output_epoch": self.output_epoch,
            "correlation_id": self.correlation_id,
            "fields": self.fields,
        }


class SessionTimeline:
    """Ordered event plane for one live session.

    Thread-safe: producers emit from the event loop, model worker threads,
    and playback-ACK handlers alike. Events are kept in a bounded ring and
    optionally appended to a JSONL file so a run can be reconstructed after
    the session ends.
    """

    def __init__(
        self,
        session_id: str,
        *,
        log_path: str | Path | None = None,
        max_events: int = MAX_TIMELINE_EVENTS,
    ) -> None:
        if not session_id:
            raise ValueError("session_id is required")
        if max_events < 1:
            raise ValueError("max_events must be positive")
        self.session_id = session_id
        self._events: list[TimelineEvent] = []
        self._seq = 0
        self._call_epoch = 0
        self._output_epoch = 0
        self._max_events = max_events
        self._lock = RLock()
        self._log_path = Path(log_path).expanduser() if log_path else None
        if self._log_path is not None:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def call_epoch(self) -> int:
        with self._lock:
            return self._call_epoch

    @property
    def output_epoch(self) -> int:
        with self._lock:
            return self._output_epoch

    def note_call_epoch(self, epoch: int) -> None:
        with self._lock:
            self._call_epoch = max(0, int(epoch))

    def note_output_epoch(self, epoch: int) -> None:
        with self._lock:
            self._output_epoch = max(0, int(epoch))

    def emit(
        self,
        kind: str,
        *,
        component: str,
        fields: dict[str, Any] | None = None,
        source_ts_ms: int | None = None,
        correlation_id: str | None = None,
        output_epoch: int | None = None,
    ) -> TimelineEvent:
        if not kind or not component:
            raise ValueError("timeline events require kind and component")
        with self._lock:
            self._seq += 1
            event = TimelineEvent(
                seq=self._seq,
                kind=kind,
                component=component,
                session_id=self.session_id,
                recv_ts_ms=int(time.time() * 1000),
                source_ts_ms=source_ts_ms,
                call_epoch=self._call_epoch,
                output_epoch=(
                    self._output_epoch if output_epoch is None else int(output_epoch)
                ),
                correlation_id=correlation_id,
                fields=dict(fields or {}),
            )
            self._events.append(event)
            if len(self._events) > self._max_events:
                del self._events[: len(self._events) - self._max_events]
        if self._log_path is not None:
            try:
                with self._log_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
            except OSError:
                LOGGER.warning("timeline JSONL write failed", exc_info=True)
        return event

    def snapshot(self, limit: int | None = None) -> tuple[TimelineEvent, ...]:
        with self._lock:
            events = tuple(self._events)
        return events if limit is None else events[-limit:]

    def kinds(self, kind: str) -> Iterable[TimelineEvent]:
        return (event for event in self.snapshot() if event.kind == kind)
