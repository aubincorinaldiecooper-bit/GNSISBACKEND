from __future__ import annotations

import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from .errors import VisualServiceError

_RETRYABLE_STATUSES = frozenset({502, 503, 504})


@dataclass(frozen=True)
class VisualSession:
    """Host stream credentials and the planner token scoped to this session."""

    session_id: str
    stream_path: str
    stream_token: str = field(repr=False)
    planner_token: str = field(repr=False)
    protocol: str


class VisualClient:
    """Use a host token for creation, closure and attempts, or a planner token
    for that session's task, decision and state operations.
    """

    def __init__(
        self,
        base_url: str,
        api_token: str,
        *,
        timeout: float = 30.0,
        max_retries: int = 2,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must not be negative")
        _validate_base_url(base_url)
        self._api_token = api_token
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
        )
        self._max_retries = max_retries

    def __enter__(self) -> VisualClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return "VisualClient(api_token=<redacted>)"

    def close(self) -> None:
        self._http.close()

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health", authenticated=False, retry=True)

    def create_session(self) -> VisualSession:
        """Create a session with a host token and return its planner token."""

        response = self._request(
            "POST",
            "/v1/visual/sessions",
            retry=False,
        )
        try:
            stream = response["stream"]
            planner = response["planner"]
            return VisualSession(
                session_id=str(response["session_id"]),
                stream_path=str(stream["path"]),
                stream_token=str(stream["token"]),
                planner_token=str(planner["token"]),
                protocol=str(stream["protocol"]),
            )
        except (KeyError, TypeError):
            raise VisualServiceError(
                "invalid_response",
                "visual API returned an invalid session response",
                status_code=200,
            ) from None

    def close_session(self, session_id: str) -> dict[str, Any]:
        """Close a session; the host token is required."""

        return self._request(
            "DELETE",
            f"/v1/visual/sessions/{quote(session_id, safe='')}",
            retry=True,
            accept_unknown_session_after_retry=True,
        )

    def set_task(
        self,
        session_id: str,
        goal: str,
        allowed_actions: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"goal": goal}
        if allowed_actions is not None:
            payload["allowed_actions"] = list(allowed_actions)
        return self._request(
            "PUT",
            f"/v1/visual/sessions/{quote(session_id, safe='')}/task",
            json=payload,
            retry=True,
        )

    def reset_task(self, session_id: str) -> dict[str, Any]:
        return self._request(
            "DELETE",
            f"/v1/visual/sessions/{quote(session_id, safe='')}/task",
            retry=True,
        )

    def decide(
        self,
        session_id: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        stable_request_id = request_id if request_id is not None else uuid.uuid4().hex
        return self._request(
            "POST",
            f"/v1/visual/sessions/{quote(session_id, safe='')}/decisions",
            json={"request_id": stable_request_id},
            retry=True,
        )

    def perceive(
        self,
        session_id: str,
        request_id: str | None = None,
        focus: str | None = None,
        target: tuple[int, int] | None = None,
    ) -> dict[str, Any]:
        """Describe the current screen; ``target`` is a viewport pixel (x, y) to ground."""

        stable_request_id = request_id if request_id is not None else uuid.uuid4().hex
        payload: dict[str, Any] = {"request_id": stable_request_id}
        if focus is not None:
            payload["focus"] = focus
        if target is not None:
            payload["target"] = {"x": int(target[0]), "y": int(target[1])}
        return self._request(
            "POST",
            f"/v1/visual/sessions/{quote(session_id, safe='')}/perceptions",
            json=payload,
            retry=True,
        )

    def record_attempt(
        self,
        session_id: str,
        decision_id: str,
    ) -> dict[str, Any]:
        """Record a host-executed attempt; the host token is required."""

        return self._request(
            "POST",
            f"/v1/visual/sessions/{quote(session_id, safe='')}/attempts",
            json={"decision_id": decision_id},
            retry=False,
        )

    def state(self, session_id: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/v1/visual/sessions/{quote(session_id, safe='')}",
            retry=True,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        authenticated: bool = True,
        retry: bool,
        accept_unknown_session_after_retry: bool = False,
    ) -> dict[str, Any]:
        headers = (
            {"Authorization": f"Bearer {self._api_token}"} if authenticated else {}
        )
        attempts = self._max_retries + 1 if retry else 1
        for attempt in range(attempts):
            try:
                response = self._http.request(
                    method,
                    path,
                    headers=headers,
                    json=json,
                )
            except httpx.TransportError:
                if attempt + 1 < attempts:
                    time.sleep(0.2 * (2**attempt))
                    continue
                raise VisualServiceError(
                    "transport_error",
                    "visual API request failed due to a network or timeout error",
                    retryable=True,
                ) from None

            if response.is_success:
                try:
                    value = response.json()
                except ValueError:
                    raise VisualServiceError(
                        "invalid_response",
                        "visual API returned invalid JSON",
                        status_code=response.status_code,
                    ) from None
                if not isinstance(value, dict):
                    raise VisualServiceError(
                        "invalid_response",
                        "visual API returned an invalid response",
                        status_code=response.status_code,
                    )
                return value

            retryable = response.status_code in _RETRYABLE_STATUSES
            if retry and retryable and attempt + 1 < attempts:
                time.sleep(0.2 * (2**attempt))
                continue
            error = self._response_error(response, retryable=retryable)
            if (
                accept_unknown_session_after_retry
                and attempt > 0
                and response.status_code == 404
                and error.code == "unknown_session"
            ):
                return {"closed": True}
            raise error

        raise AssertionError("request attempts exhausted without a response")

    def _response_error(
        self,
        response: httpx.Response,
        *,
        retryable: bool,
    ) -> VisualServiceError:
        code = f"http_{response.status_code}"
        message = f"visual API request failed with status {response.status_code}"
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            detail = body.get("error", body.get("detail"))
            if isinstance(detail, dict):
                code = detail.get("code", code)
                message = detail.get("message", message)
        return VisualServiceError(
            _redact(str(code), self._api_token),
            _redact(str(message), self._api_token),
            status_code=response.status_code,
            retryable=retryable,
        )


def _redact(value: str, *secrets: str) -> str:
    for secret in secrets:
        if secret:
            value = value.replace(secret, "[redacted]")
    return value


def _validate_base_url(base_url: str) -> None:
    try:
        parsed = urlsplit(base_url)
        hostname = parsed.hostname
    except ValueError:
        raise VisualServiceError(
            "invalid_base_url",
            "visual API base URL is invalid",
        ) from None
    scheme = parsed.scheme.lower()
    if scheme == "https" or (
        scheme == "http"
        and hostname
        in {
            "localhost",
            "127.0.0.1",
            "::1",
        }
    ):
        return
    raise VisualServiceError(
        "insecure_transport",
        "visual API requires HTTPS except for loopback hosts",
    )
