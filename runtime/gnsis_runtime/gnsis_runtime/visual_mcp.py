from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from mcp.server import MCPServer


@dataclass(frozen=True)
class VisualMCPConfig:
    """MCP planners use the session-scoped planner token returned to the host."""

    api_base: str
    api_token: str = field(repr=False)
    session_id: str
    timeout_sec: float = 30.0

    @classmethod
    def from_env(cls) -> "VisualMCPConfig":
        api_base = os.environ.get("GNSIS_VISUAL_API_BASE", "").rstrip("/")
        api_token = os.environ.get("GNSIS_VISUAL_API_TOKEN", "")
        session_id = os.environ.get("GNSIS_VISUAL_SESSION_ID", "")
        missing = [
            name
            for name, value in (
                ("GNSIS_VISUAL_API_BASE", api_base),
                ("GNSIS_VISUAL_API_TOKEN", api_token),
                ("GNSIS_VISUAL_SESSION_ID", session_id),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                f"missing Smaller GNSIS MCP configuration: {', '.join(missing)}"
            )
        try:
            hostname = urlsplit(api_base).hostname
        except ValueError:
            hostname = None
        if api_base.lower().startswith("http://") and hostname not in {
            "localhost",
            "127.0.0.1",
            "::1",
        }:
            raise RuntimeError(
                "Smaller GNSIS MCP requires HTTPS except for loopback hosts"
            )
        return cls(
            api_base=api_base,
            api_token=api_token,
            session_id=session_id,
        )


class VisualAPIClient:
    """Forward planner operations with a session-scoped planner credential."""

    def __init__(self, config: VisualMCPConfig) -> None:
        self.config = config

    def set_task(
        self,
        goal: str,
        allowed_actions: list[str] | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {"goal": goal}
        if allowed_actions is not None:
            payload["allowed_actions"] = allowed_actions
        return self._request("PUT", "/task", payload)

    def decide(self, request_id: str) -> dict[str, object]:
        return self._request("POST", "/decisions", {"request_id": request_id})

    def state(self) -> dict[str, object]:
        return self._request("GET", "")

    def reset(self) -> dict[str, object]:
        return self._request("DELETE", "/task")

    def _request(
        self,
        method: str,
        suffix: str,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        body = None
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode()
        url = (
            f"{self.config.api_base}/v1/visual/sessions/"
            f"{self.config.session_id}{suffix}"
        )
        request = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.config.api_token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=self.config.timeout_sec,
            ) as response:
                value = json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                error = json.load(exc)
            except (json.JSONDecodeError, UnicodeDecodeError):
                error = {"error": {"code": "http_error", "message": str(exc)}}
            raise RuntimeError(json.dumps(error, separators=(",", ":"))) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Smaller GNSIS API unavailable: {exc.reason}") from exc
        if not isinstance(value, dict):
            raise RuntimeError("Smaller GNSIS API returned a non-object response")
        return value


def build_server(config: VisualMCPConfig | None = None) -> MCPServer:
    client = VisualAPIClient(config or VisualMCPConfig.from_env())
    server = MCPServer(
        "smaller-gnsis",
        description=(
            "Fast visual action decisions over a host-owned live screen stream. "
            "Use a session-scoped planner token. The host alone captures and "
            "executes actions."
        ),
        version="1.0.0",
    )

    @server.tool()
    def visual_set_task(
        goal: str,
        allowed_actions: list[str] | None = None,
    ) -> dict[str, object]:
        """Set the current visual goal and code-bounded legal action set."""

        return client.set_task(goal, allowed_actions)

    @server.tool()
    def visual_decide(request_id: str) -> dict[str, object]:
        """Return one grounded action for the current live visual frame.

        Reuse the same request_id when retrying a timed-out request.
        """

        return client.decide(request_id)

    @server.tool()
    def visual_state() -> dict[str, object]:
        """Inspect task, bounded history, current frame and usage state."""

        return client.state()

    @server.tool()
    def visual_reset() -> dict[str, object]:
        """Clear the current goal and visual action history."""

        return client.reset()

    return server


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
