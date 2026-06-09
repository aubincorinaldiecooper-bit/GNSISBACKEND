"""Tracer — records a single run's trajectory.

Every agent step (model response, tool call, tool result) is captured as an
event so a run can be replayed or inspected after the fact. This is the
'Observe' substrate: trajectories are the raw material the optimizer reasons
about.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class TraceEvent:
    kind: str
    data: Dict[str, Any] = field(default_factory=dict)
    ts: str = field(default_factory=_now)


class Tracer:
    def __init__(self, workdir: Optional[str] = None, run_id: Optional[str] = None) -> None:
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.workdir = workdir
        self.started_at = _now()
        self.events: List[TraceEvent] = []

    def event(self, kind: str, data: Optional[Dict[str, Any]] = None) -> TraceEvent:
        evt = TraceEvent(kind=kind, data=data or {})
        self.events.append(evt)
        return evt

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "events": [asdict(e) for e in self.events],
        }

    def save(self) -> Optional[str]:
        if not self.workdir:
            return None
        directory = os.path.join(self.workdir, "traces")
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"{self.run_id}.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, indent=2, ensure_ascii=False)
        return path
