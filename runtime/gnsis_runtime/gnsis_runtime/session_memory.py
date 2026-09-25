"""Server-owned durable recall for live duplex sessions.

Per AGENTS.md the browser/desktop client must not synthesize ``memory.episode``
control messages as the permanent recall path: the runtime itself queries the
GNSIS Omni-SimpleMem sidecar when a user turn lands and feeds compact,
relevant memories into the model's pinned context through the existing
``inject_memory_episode`` channel.

A recalled memory becomes a ``MemoryEpisode`` (``key``/``start_sec``/
``end_sec``/``event_summary``); the source-time range travels in the summary
and provenance so the pinned-context compressor treats it like any other
episode without pretending it is a live-context interval.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

LOGGER = logging.getLogger(__name__)

EPISODE_SUMMARY_CHARS = 400


class SessionMemoryRecall:
    """Query the Omni-SimpleMem sidecar and shape hits as context episodes."""

    def __init__(
        self,
        base_url: str,
        *,
        namespace: str,
        token: str = "",
        top_k: int = 8,
        timeout_s: float = 10.0,
    ) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("session recall URL must be an absolute http(s) URL")
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError("session recall URL must not carry credentials or a fragment")
        if not namespace or len(namespace) > 200:
            raise ValueError("session recall namespace must be 1-200 chars")
        if top_k < 1:
            raise ValueError("session recall top_k must be positive")
        if timeout_s <= 0:
            raise ValueError("session recall timeout must be positive")
        self.base_url = base_url.rstrip("/")
        self.namespace = namespace
        self.token = token
        self.top_k = top_k
        self.timeout_s = timeout_s

    def search(self, query: str) -> list[dict[str, Any]]:
        """Return raw sidecar hits for ``query`` in the configured namespace."""
        body = json.dumps(
            {"query": query, "top_k": self.top_k},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.token:
            headers["X-GNSIS-SimpleMem-Token"] = self.token
        request = Request(
            f"{self.base_url}/namespaces/{quote(self.namespace, safe='')}/query",
            data=body,
            headers=headers,
            method="POST",
        )
        start = time.monotonic()
        try:
            with urlopen(request, timeout=self.timeout_s) as response:
                decoded = json.loads(response.read(2 * 1024 * 1024))
        except (HTTPError, URLError, OSError, ValueError) as exc:
            LOGGER.warning(
                "session.recall op=query latency_ms=%.1f status=error",
                (time.monotonic() - start) * 1000,
            )
            raise RuntimeError(f"session memory recall failed: {exc}") from exc
        items = decoded.get("items")
        if not isinstance(items, list):
            raise RuntimeError("session memory recall reply is missing items")
        hits = [item for item in items if isinstance(item, dict)]
        LOGGER.info(
            "session.recall op=query latency_ms=%.1f status=ok hits=%d",
            (time.monotonic() - start) * 1000,
            len(hits),
        )
        return hits

    def episodes_for_turn(self, turn_text: str) -> list[dict[str, Any]]:
        """Shape relevant memories as ``memory.episode`` entries.

        Each episode keeps the memory's identity in ``key`` so a re-recalled
        hit is a no-op instead of duplicate context, and carries provenance in
        the summary so the model sees *what* is known plus *where it came
        from*.
        """
        query = turn_text.strip()
        if not query:
            return []
        try:
            items = self.search(query)
        except Exception:
            # Recall failure must never break the live turn. The memory simply
            # does not participate this turn; the error is logged for telemetry.
            LOGGER.warning("session memory recall failed", exc_info=True)
            return []
        episodes: list[dict[str, Any]] = []
        now_sec = time.time()
        for item in items:
            summary = str(item.get("summary") or item.get("text") or "").strip()
            memory_id = item.get("mauId") or item.get("ref")
            if not summary or not memory_id:
                continue
            provenance = item.get("provenance")
            prov = provenance if isinstance(provenance, dict) else {}
            start = prov.get("source_start_sec") or prov.get("sourceStartSec")
            end = prov.get("source_end_sec") or prov.get("sourceEndSec")
            memory_type = item.get("type") or "episode"
            prefix = f"[{memory_type}]"
            if prov.get("source_session_id") or prov.get("sourceSessionId"):
                prefix += f"[session {prov.get('source_session_id') or prov.get('sourceSessionId')}]"
            if isinstance(start, (int, float)) and isinstance(end, (int, float)):
                prefix += f"[{float(start):.0f}s-{float(end):.0f}s]"
            episodes.append(
                {
                    "key": f"recall:{memory_id}",
                    # Recalled memories are not intervals of the current
                    # context, but the pinned-context schema requires a finite
                    # start<end. Give them a degenerate 1s span at the recall
                    # moment; they never cover dropped history because no live
                    # capture shares that range unless the timing collides —
                    # which is acceptable: a covered segment still gets the
                    # episode as its summary.
                    "start_sec": now_sec - 1.0,
                    "end_sec": now_sec,
                    "event_summary": f"{prefix} {summary}"[:EPISODE_SUMMARY_CHARS],
                }
            )
        LOGGER.info(
            "session.recall op=episodes namespace=%s query_chars=%d episodes=%d",
            self.namespace,
            len(query),
            len(episodes),
        )
        return episodes
