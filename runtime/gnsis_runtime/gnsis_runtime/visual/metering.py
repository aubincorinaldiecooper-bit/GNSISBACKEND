from __future__ import annotations

import json
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any, Protocol
from urllib import error as urllib_error
from urllib.parse import urlparse
from urllib.request import Request, urlopen


@dataclass(frozen=True, slots=True)
class UsageReport:
    workspace_id: str
    virtual_key_id: str
    project_id: str | None
    environment_id: str | None
    grant_id: str
    session_id: str
    report_seq: int
    frames_accepted: int
    frame_bytes: int
    decisions: int
    decisions_act: int
    decisions_abstain: int
    attempts_recorded: int
    inference_ms: int
    session_ms: int
    closed: bool
    generated_at_ms: int = 0
    perceptions: int = 0
    inspections: int = 0
    pixel_reads: int = 0
    history_reads: int = 0
    host_client: str | None = None
    planner_client: str | None = None

    @property
    def event_id(self) -> str:
        return f"{self.session_id}:{self.report_seq}"

    def to_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["event_id"] = self.event_id
        return payload


class UsageSink(Protocol):
    def start(self, service: Any) -> None: ...

    def stop(self) -> None: ...

    def health(self) -> dict[str, Any]: ...


def _validate_usage_url(url: str) -> None:
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    if scheme == "https":
        return
    if scheme == "http" and hostname in {"localhost", "127.0.0.1", "::1"}:
        return
    raise ValueError("visual usage URL must use HTTPS (or loopback HTTP)")


class HttpUsageSink:
    def __init__(
        self,
        url: str,
        secret: str,
        *,
        post: Callable[[bytes, dict[str, str]], None] | None = None,
        interval_s: float = 10.0,
        max_pending: int = 10_000,
    ) -> None:
        _validate_usage_url(url)
        if not secret:
            raise ValueError("visual usage secret must not be empty")
        if interval_s <= 0:
            raise ValueError("usage sink interval must be positive")
        if max_pending < 1:
            raise ValueError("usage sink max_pending must be positive")
        self.url = url
        self.secret = secret
        self.post = post or self._post_http
        self.interval_s = interval_s
        self.max_pending = max_pending
        self._pending: deque[UsageReport] = deque()
        self._dropped = 0
        self._failures = 0
        self._last_error: str | None = None
        self._delivery_failed = False
        self._lock = threading.RLock()
        self._flush_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._service: Any | None = None
        self.max_batch_reports = 500

    def start(self, service: Any) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._service = service
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="visual-usage-sink",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
        if thread is None:
            return
        self._stop.set()
        thread.join(timeout=max(1.0, self.interval_s + 1.0))
        for _ in range(3):
            self._flush()
            with self._lock:
                if not self._pending:
                    break
            self._stop.wait(0.5)
        with self._lock:
            self._thread = None

    def health(self) -> dict[str, Any]:
        with self._lock:
            return {
                "pending": len(self._pending),
                "dropped": self._dropped,
                "failures": self._failures,
                "last_error": self._last_error,
                "delivery_failed": self._delivery_failed,
                "running": self._thread is not None and self._thread.is_alive(),
            }

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            self._flush()

    def _flush(self) -> None:
        with self._flush_lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        service = self._service
        if service is None:
            return
        reports = service.collect_usage()
        with self._lock:
            self._pending.extend(reports)
            while len(self._pending) > self.max_pending:
                self._pending.popleft()
                self._dropped += 1
        while True:
            with self._lock:
                batch = list(self._pending)[: self.max_batch_reports]
            if not batch:
                return
            payload = json.dumps(
                {"reports": [report.to_json() for report in batch]},
                separators=(",", ":"),
            ).encode()
            try:
                self.post(
                    payload,
                    {
                        "Authorization": f"Bearer {self.secret}",
                        "Content-Type": "application/json",
                    },
                )
            except (OSError, urllib_error.URLError, ValueError) as exc:
                with self._lock:
                    self._failures += 1
                    self._last_error = type(exc).__name__
                    self._delivery_failed = True
                return
            with self._lock:
                for _ in range(min(len(batch), len(self._pending))):
                    self._pending.popleft()
                self._last_error = None
                self._delivery_failed = False

    def _post_http(self, payload: bytes, headers: dict[str, str]) -> None:
        request = Request(self.url, data=payload, headers=headers, method="POST")
        with urlopen(request, timeout=10):
            return
