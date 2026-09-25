"""Thin boundary between GNSIS and the Qwen Live Harness daemon/ACP subsystem.

Per AGENTS.md decision 8: direct reuse of the QLH daemon — GNSIS owns session
IDs, memory, policy, and the realtime provider; the daemon is only the
background-agent execution backend. Two pieces:

- ``HarnessDaemonClient`` — speaks the daemon's external control surface over
  HTTP: ``GET /live/instance``, ``GET /healthz``, ``POST /live/subagents``
  (``list`` / ``stop`` / ``permission``), ``POST /live/quit``. Auth is
  ``Authorization: Bearer <token>`` plus the ``x-qwen-live-harness-nonce``
  header, both read from the daemon's discovery file
  (``{url, protocolVersion, pid, instanceNonce, token}``, mode 0600).
- ``HarnessBridge`` — polls the subagent snapshot, diffs it onto the GNSIS
  ``SessionTimeline`` (task.delegated/progress/terminal, permission.requested),
  and on terminal task states enqueues the result into the existing Gateway
  delivery lane (``DeliveryBroker.enqueue``, ``topic="final"``). The existing
  delivery loop then applies the delivery gate — user not speaking, playback
  drained, current epoch — exactly like worker deliveries. No parallel
  result-delivery path exists.

Permission requests from a backend are a state transition (timeline event +
pending surface), never an implicit grant: decisions go back through
``POST /live/subagents {action: "permission"}``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .timeline import SessionTimeline

LOGGER = logging.getLogger(__name__)

_TERMINAL = {"completed", "failed", "cancelled", "interrupted"}
_MAX_DISCOVERY_BYTES = 64 * 1024


class HarnessUnavailable(Exception):
    """The Harness daemon could not be reached or answered badly."""


@dataclass(frozen=True)
class HarnessDiscovery:
    """A QLH daemon discovery record, validated like the Host does."""

    url: str
    protocol_version: int
    pid: int
    instance_nonce: str
    token: str | None = None
    config_path: str | None = None

    @staticmethod
    def load(path: str | Path) -> "HarnessDiscovery":
        p = Path(path).expanduser()
        st = os.lstat(p)
        if not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode):
            raise HarnessUnavailable(f"discovery is not a regular file: {p}")
        if stat.S_IMODE(st.st_mode) != 0o600:
            raise HarnessUnavailable(f"discovery must be mode 0600: {p}")
        if st.st_size <= 0 or st.st_size > _MAX_DISCOVERY_BYTES:
            raise HarnessUnavailable(f"discovery has implausible size: {p}")
        try:
            value = json.loads(p.read_text())
        except (OSError, ValueError) as exc:
            raise HarnessUnavailable(f"discovery unreadable: {exc}") from exc
        if not isinstance(value, dict):
            raise HarnessUnavailable("discovery is not a JSON object")
        url = value.get("url")
        nonce = value.get("instanceNonce")
        protocol = value.get("protocolVersion")
        pid = value.get("pid")
        token = value.get("token")
        config_path = value.get("configPath")
        if (
            not isinstance(url, str)
            or not url.startswith(("http://", "https://"))
            or not isinstance(nonce, str)
            or not nonce
            or not isinstance(protocol, int)
            or not isinstance(pid, int)
            or pid <= 0
            or (token is not None and not isinstance(token, str))
            or (config_path is not None and not isinstance(config_path, str))
        ):
            raise HarnessUnavailable("discovery has an invalid shape")
        return HarnessDiscovery(
            url=url,
            protocol_version=protocol,
            pid=pid,
            instance_nonce=nonce,
            token=token,
            config_path=config_path,
        )


@dataclass
class HarnessDaemonClient:
    """Qwen Live Harness daemon control-surface client (stdlib urllib only)."""

    discovery: HarnessDiscovery
    timeout_sec: float = 10.0

    @classmethod
    def from_discovery_file(
        cls, path: str | Path, *, timeout_sec: float = 10.0
    ) -> "HarnessDaemonClient":
        return cls(HarnessDiscovery.load(path), timeout_sec=timeout_sec)

    # -- HTTP plumbing ---------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {"x-qwen-live-harness-nonce": self.discovery.instance_nonce}
        if self.discovery.token:
            headers["authorization"] = f"Bearer {self.discovery.token}"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        url = f"{self.discovery.url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, method=method, data=data)
        for key, value in self._headers().items():
            req.add_header(key, value)
        if data is not None:
            req.add_header("content-type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as res:
                payload = res.read()
        except urllib.error.HTTPError as exc:
            raise HarnessUnavailable(f"{method} {path} -> HTTP {exc.code}") from exc
        except OSError as exc:
            raise HarnessUnavailable(f"{method} {path} failed: {exc}") from exc
        if not payload:
            return None
        try:
            return json.loads(payload)
        except ValueError as exc:
            raise HarnessUnavailable(f"{method} {path} returned non-JSON") from exc

    # -- control surface --------------------------------------------------------

    def healthz(self) -> bool:
        try:
            self._request("GET", "/healthz")
        except HarnessUnavailable:
            return False
        return True

    def instance(self) -> dict[str, Any]:
        value = self._request("GET", "/live/instance")
        if not isinstance(value, dict):
            raise HarnessUnavailable("/live/instance returned a non-object")
        return value

    def list_subagents(
        self,
        *,
        offset: int | None = None,
        selected_id: str | None = None,
    ) -> dict[str, Any]:
        action: dict[str, Any] = {"action": "list"}
        if offset is not None:
            action["offset"] = offset
        if selected_id is not None:
            action["selectedId"] = selected_id
        result = self._request("POST", "/live/subagents", action)
        if not isinstance(result, dict):
            raise HarnessUnavailable("subagents list returned a non-object")
        if result.get("type") == "error":
            raise HarnessUnavailable(f"subagents list error: {result.get('code')}")
        return result

    def stop_task(self, task_id: str) -> dict[str, Any]:
        result = self._request(
            "POST", "/live/subagents", {"action": "stop", "taskId": task_id}
        )
        if not isinstance(result, dict):
            raise HarnessUnavailable("subagents stop returned a non-object")
        if result.get("type") == "error":
            raise HarnessUnavailable(f"subagents stop error: {result.get('code')}")
        return result

    def decide_permission(
        self,
        request_handle: str,
        decision: str,
        *,
        scope: str | None = None,
    ) -> dict[str, Any]:
        if decision not in {"allow", "deny"}:
            raise ValueError("permission decision must be allow or deny")
        action: dict[str, Any] = {
            "action": "permission",
            "requestHandle": request_handle,
            "decision": decision,
        }
        if scope is not None:
            action["scope"] = scope
        result = self._request("POST", "/live/subagents", action)
        if not isinstance(result, dict):
            raise HarnessUnavailable("subagents permission returned a non-object")
        if result.get("type") == "error":
            raise HarnessUnavailable(
                f"subagents permission error: {result.get('code')}"
            )
        return result

    def quit(self) -> None:
        self._request("POST", "/live/quit")


@dataclass
class HarnessBridge:
    """Polls Harness subagent state onto the timeline + Gateway delivery lane.

    ``coordinator`` is the session's TaskToolsRealtimeCoordinator: its
    ``gateway.delivery.enqueue`` is the *only* path results take toward
    speech, which is what makes the delivery gate apply uniformly.
    """

    coordinator: Any
    client: HarnessDaemonClient
    poll_sec: float = 2.0
    timeline: SessionTimeline | None = None
    max_speech_hint_chars: int = 2_000

    _task: asyncio.Task[Any] | None = field(default=None, init=False, repr=False)
    _known: dict[str, str] = field(default_factory=dict, init=False)
    _seen_permissions: set[str] = field(default_factory=set, init=False)
    _closed: bool = field(default=False, init=False)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._closed = False
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    # -- internals ---------------------------------------------------------------

    def _timeline(self) -> SessionTimeline:
        return self.timeline or self.coordinator.timeline

    async def _run(self) -> None:
        while not self._closed:
            try:
                page = await asyncio.to_thread(self.client.list_subagents)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.warning("harness subagents poll failed", exc_info=True)
                self._timeline().emit(
                    "harness.poll_failed", component="harness"
                )
            else:
                snapshot = page.get("page", {}).get("snapshot")
                if isinstance(snapshot, dict):
                    self._diff(snapshot)
            await asyncio.sleep(self.poll_sec)

    def _diff(self, snapshot: Mapping[str, Any]) -> None:
        timeline = self._timeline()
        timeline.emit(
            "harness.snapshot",
            component="harness",
            fields={
                "revision": snapshot.get("revision"),
                "counts": snapshot.get("counts"),
            },
        )
        tasks = snapshot.get("tasks")
        if not isinstance(tasks, list):
            return
        for task in tasks:
            if not isinstance(task, dict):
                continue
            task_id = str(task.get("id", ""))
            if not task_id:
                continue
            status = str(task.get("status", "unknown"))
            previous = self._known.get(task_id)
            self._known[task_id] = status
            if previous is None:
                timeline.emit(
                    "task.delegated",
                    component="harness",
                    correlation_id=task_id,
                    fields={
                        "kind": task.get("kind"),
                        "backend": task.get("backend"),
                        "title": task.get("title"),
                        "status": status,
                    },
                )
            elif previous != status:
                timeline.emit(
                    "task.progress",
                    component="harness",
                    correlation_id=task_id,
                    fields={
                        "from": previous,
                        "to": status,
                        "activity": task.get("activity"),
                    },
                )
            for permission in task.get("permissions") or []:
                handle = (
                    permission.get("requestHandle")
                    if isinstance(permission, dict)
                    else None
                )
                if handle and handle not in self._seen_permissions:
                    self._seen_permissions.add(handle)
                    timeline.emit(
                        "permission.requested",
                        component="harness",
                        correlation_id=str(handle),
                        fields={
                            "task_id": task_id,
                            "backend": permission.get("backend"),
                            "title": permission.get("title"),
                        },
                    )
            if status in _TERMINAL and previous != status:
                self._enqueue_result(task)
        # Forget tasks that dropped out of the snapshot window entirely.
        live_ids = {
            str(t.get("id", "")) for t in tasks if isinstance(t, dict)
        }
        for task_id in list(self._known):
            if task_id not in live_ids:
                del self._known[task_id]

    def _enqueue_result(self, task: Mapping[str, Any]) -> None:
        task_id = str(task.get("id", ""))
        status = str(task.get("status", "completed"))
        output = task.get("output")
        if not isinstance(output, str) or not output.strip():
            output = task.get("outputMessage") or task.get("title") or task_id
        hint = str(output).strip()[: self.max_speech_hint_chars]
        timeline = self._timeline()
        delivery = self.coordinator.gateway.delivery.enqueue(
            owner_id=self.coordinator.owner_id,
            task_id=task_id,
            event_id=None,
            interaction_id=None,
            timing="safe_pause",
            topic="final",
            speech_hint=hint,
            dedupe_key=f"harness-result:{task_id}:{status}",
            status=status if status in {"completed", "failed", "cancelled"} else "failed",
        )
        timeline.emit(
            "result_ready",
            component="harness",
            correlation_id=task_id,
            fields={
                "task_status": status,
                "delivery_id": delivery.delivery_id if delivery else None,
                "queued": delivery is not None,
            },
        )
        # The delivery lifecycle (awaiting_delivery -> delivering -> delivered)
        # is tracked by the gate against this delivery_id in _delivery_loop;
        # registering the task id itself gives the task-level lifecycle parity.
        self.coordinator.gate.register(
            f"harness:{task_id}",
            delivery_id=delivery.delivery_id if delivery else None,
        )


async def attach_harness(
    coordinator: Any,
    *,
    discovery_path: str | None,
    poll_sec: float = 2.0,
    timeout_sec: float = 10.0,
) -> HarnessBridge | None:
    """Build + start a bridge for one session; None when unconfigured/down."""

    if not discovery_path:
        return None
    try:
        client = HarnessDaemonClient.from_discovery_file(
            discovery_path, timeout_sec=timeout_sec
        )
        instance = await asyncio.to_thread(client.instance)
    except (HarnessUnavailable, OSError) as exc:
        LOGGER.warning("harness daemon unreachable: %s", exc)
        coordinator.timeline.emit(
            "harness.unavailable",
            component="harness",
            fields={"detail": str(exc)[:200]},
        )
        return None
    coordinator.timeline.emit(
        "harness.connected",
        component="harness",
        fields={
            "protocol_version": instance.get("protocolVersion"),
            "version": instance.get("version"),
        },
    )
    bridge = HarnessBridge(coordinator, client, poll_sec=poll_sec)
    bridge.start()
    return bridge
