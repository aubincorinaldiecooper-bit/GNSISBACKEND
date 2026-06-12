"""Memory — the 'Remember' phase of the evolution cycle.

A simple append-only JSONL log of events the runtime wants to persist across
runs (which prompt won, what it scored, why). Small on purpose: durable,
inspectable, and free of dependencies.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


class Memory:
    def __init__(self, workdir: str, namespace: str = "default") -> None:
        safe_ns = _SAFE.sub("_", namespace)
        self.path = os.path.join(workdir, "memory", f"{safe_ns}.jsonl")

    def remember(self, event: Dict[str, Any]) -> Dict[str, Any]:
        record = dict(event)
        record.setdefault("ts", datetime.now(timezone.utc).isoformat())
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def recall(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        if not os.path.exists(self.path):
            return []
        with open(self.path, "r", encoding="utf-8") as handle:
            events = [json.loads(line) for line in handle if line.strip()]
        return events[-limit:] if limit else events

    def clear(self) -> None:
        if os.path.exists(self.path):
            os.remove(self.path)

    def __len__(self) -> int:
        return len(self.recall())
