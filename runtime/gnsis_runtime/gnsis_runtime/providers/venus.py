"""Realtime-Venus provider — HTTP client of the VenusOmni model server.

The upstream ``demos/model`` server already owns session incarnation,
generation epochs, output IDs, and playback acknowledgements
(``POST /sessions/{sid}/playback_ack``), so this adapter only translates
its wire protocol into the normalized ``RealtimeSession`` surface. Model
weights live wherever the server runs; this process stays a thin client.

Upstream reference: inclusionAI/Realtime-Venus, demos/model/server.py +
demos/model/wire.py (revision e53ae8d, Apache-2.0).
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.request
from typing import Any

from ..realtime_provider import (
    ProviderEvent,
    ProviderSessionConfig,
    RealtimeSession,
)

LOGGER = logging.getLogger("gnsis_runtime.providers.venus")


class VenusUnavailable(RuntimeError):
    """The Venus model server is unreachable or not ready."""


class VenusRealtimeProvider:
    """Open Venus sessions against a running VenusOmni ServingPort server."""

    def __init__(self, base_url: str, *, timeout_s: float = 30.0) -> None:
        base_url = (base_url or "").strip().rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("venus provider requires an absolute http(s) base_url")
        if timeout_s <= 0:
            raise ValueError("venus provider timeout must be positive")
        self.base_url = base_url
        self.timeout_s = timeout_s

    @property
    def provider_name(self) -> str:
        return "venus"

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        query: str = "",
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        if query:
            url += f"?{query}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8")[:200]
            except Exception:
                pass
            raise VenusUnavailable(
                f"venus {method} {path} -> {exc.code} {detail}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise VenusUnavailable(f"venus {method} {path} unreachable: {exc}") from exc
        if not isinstance(payload, dict):
            raise VenusUnavailable(f"venus {method} {path} -> non-object response")
        if "error" in payload:
            raise VenusUnavailable(
                f"venus {method} {path} -> {payload.get('type', 'Error')}: {payload['error']}"
            )
        return payload

    async def open_session(
        self, config: ProviderSessionConfig
    ) -> "VenusRealtimeSession":
        import asyncio

        opened = await asyncio.to_thread(
            self._request,
            "POST",
            "/sessions",
            {
                "session_id": config.session_id,
                "input_sample_rate": config.input_sample_rate,
                "output_sample_rate": config.output_sample_rate,
                "system_prompt": config.system_prompt,
                "ref_audio_path": config.ref_audio_path,
                **config.extra,
            },
        )
        incarnation = int(opened.get("incarnation", 0))
        LOGGER.info(
            "venus.session op=open session=%s incarnation=%s",
            config.session_id,
            incarnation,
        )
        return VenusRealtimeSession(
            self, session_id=config.session_id, incarnation=incarnation
        )

    async def health(self) -> dict[str, Any]:
        import asyncio

        return await asyncio.to_thread(self._request, "GET", "/healthz")

    async def close(self) -> None:
        return None


class VenusRealtimeSession:
    """One live session on the Venus model server."""

    def __init__(
        self,
        provider: VenusRealtimeProvider,
        *,
        session_id: str,
        incarnation: int,
    ) -> None:
        self._provider = provider
        self._session_id = session_id
        self._incarnation = incarnation
        self._closed = False

    @property
    def session_id(self) -> str:
        return self._session_id

    def _path(self, suffix: str = "") -> str:
        return f"/sessions/{self._session_id}{suffix}"

    def _incarnate(self, body: dict[str, Any]) -> dict[str, Any]:
        body["session_id"] = self._session_id
        body["incarnation"] = self._incarnation
        return body

    async def push_audio(
        self, pcm16: bytes, *, capture_ts_ms: int | None = None
    ) -> None:
        import asyncio

        body: dict[str, Any] = self._incarnate(
            {"data": base64.b64encode(pcm16).decode("ascii")}
        )
        if capture_ts_ms is not None:
            body["capture_ts_ms"] = int(capture_ts_ms)
        await asyncio.to_thread(
            self._provider._request, "POST", self._path("/audio"), body
        )

    async def push_video_frame(
        self, data: bytes, *, mime_type: str = "image/jpeg", ts_ms: int | None = None
    ) -> None:
        import asyncio

        body: dict[str, Any] = self._incarnate(
            {
                "data": base64.b64encode(data).decode("ascii"),
                "mime_type": mime_type,
            }
        )
        if ts_ms is not None:
            body["ts_ms"] = int(ts_ms)
        await asyncio.to_thread(
            self._provider._request, "POST", self._path("/video_frame"), body
        )

    async def push_control(self, control: dict[str, Any]) -> None:
        import asyncio

        kind = control.get("kind", "prefill")
        if kind == "prefill":
            body = self._incarnate(
                {
                    "work_id": control["work_id"],
                    "attempt_id": control["attempt_id"],
                    "text_list": control["text_list"],
                }
            )
            await asyncio.to_thread(
                self._provider._request, "POST", self._path("/prefill"), body
            )
            return
        raise ValueError(f"unsupported venus control kind: {kind!r}")

    async def next_event(self, timeout_s: float | None = None) -> ProviderEvent:
        import asyncio

        query = f"incarnation={self._incarnation}"
        if timeout_s is not None:
            query += f"&timeout_s={timeout_s}"
        try:
            item = await asyncio.to_thread(
                self._provider._request, "POST", self._path("/output"), None, query
            )
        except VenusUnavailable as exc:
            raise TimeoutError(str(exc)) from exc
        return _normalize_step(item)

    async def acknowledge_playback(
        self, output_id: str, *, chunks_played: int
    ) -> bool:
        import asyncio

        body = self._incarnate(
            {
                "utterance_id": output_id,
                "cumulative_played_chunks": int(chunks_played),
            }
        )
        resp = await asyncio.to_thread(
            self._provider._request, "POST", self._path("/playback_ack"), body
        )
        return "utterance_id" in resp or "accepted_at_ms" in resp

    async def cancel_output(self, reason: str = "cancelled") -> None:
        # Venus cancels stale generations by playback epoch; there is no
        # separate cancel endpoint — a new push/generation supersedes the old.
        LOGGER.info(
            "venus.session op=cancel_output session=%s reason=%s",
            self._session_id,
            reason,
        )

    async def close(self) -> None:
        import asyncio

        if self._closed:
            return
        self._closed = True
        await asyncio.to_thread(
            self._provider._request,
            "DELETE",
            self._path(),
            None,
            f"incarnation={self._incarnation}&reason=client_close",
        )
        LOGGER.info("venus.session op=close session=%s", self._session_id)


def _normalize_step(item: dict[str, Any]) -> ProviderEvent:
    """Map one Venus ``model_output_step`` wire dict to a ProviderEvent."""

    if "audio" in item and item.get("audio") is not None:
        kind = "audio"
        payload = {
            "pcm16": base64.b64decode(item["audio"].get("data", ""))
            if isinstance(item["audio"], dict)
            else item["audio"],
            "turn_finished": item.get("turn_finished", False),
            "finish_reason": item.get("finish_reason"),
        }
    elif "work_id" in item:
        kind = "control"
        payload = {"work_id": item["work_id"], "kv_position": item.get("kv_position")}
    elif "utterance_id" in item:
        kind = "control"
        payload = {"utterance_id": item["utterance_id"]}
    else:
        kind = "control" if not item.get("turn_finished") else "turn"
        payload = {
            "turn_finished": item.get("turn_finished"),
            "finish_reason": item.get("finish_reason"),
            "token_count": len(item.get("total_token_ids") or []),
        }
    return ProviderEvent(
        kind=kind,
        payload=payload,
        epoch=item.get("generation_epoch"),
        seq=item.get("step_seq") or item.get("applied_at_ms"),
        correlation_id=item.get("generation_id") or item.get("utterance_id"),
        raw=item,
    )
