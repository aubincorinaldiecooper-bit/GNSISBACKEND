from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlsplit

from mcp.server import MCPServer
from mcp.server.mcpserver import Image
from mcp.types import TextContent


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
            parsed = urlsplit(api_base)
            hostname = parsed.hostname
        except ValueError:
            parsed = None
            hostname = None
        scheme = parsed.scheme.lower() if parsed is not None else ""
        if scheme != "https" and not (
            scheme == "http"
            and hostname
            in {
                "localhost",
                "127.0.0.1",
                "::1",
            }
        ):
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

    def perceive(
        self,
        request_id: str,
        focus: str | None = None,
        target: tuple[int, int] | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {"request_id": request_id}
        if focus is not None:
            payload["focus"] = focus
        if target is not None:
            payload["target"] = {"x": int(target[0]), "y": int(target[1])}
        return self._request("POST", "/perceptions", payload)

    def state(self) -> dict[str, object]:
        return self._request("GET", "")

    def history(self, limit: int | None = None) -> dict[str, object]:
        query = "" if limit is None else "?" + urlencode({"limit": int(limit)})
        return self._request("GET", f"/history{query}")

    def inspect(
        self,
        frame_id: str | None = None,
        region: tuple[int, int, int, int] | None = None,
        display_size: int | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {}
        if frame_id is not None:
            payload["frame_id"] = frame_id
        if region is not None:
            payload["region"] = _region(region)
        if display_size is not None:
            payload["display_size"] = int(display_size)
        return self._request("POST", "/inspections", payload)

    def read_pixels(
        self,
        region: tuple[int, int, int, int],
        frame_id: str | None = None,
        step: int = 1,
    ) -> dict[str, object]:
        payload: dict[str, object] = {"region": _region(region), "step": int(step)}
        if frame_id is not None:
            payload["frame_id"] = frame_id
        return self._request("POST", "/pixels", payload)

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


def _region(region: tuple[int, int, int, int]) -> dict[str, int]:
    x, y, width, height = (int(value) for value in region)
    return {"x": x, "y": y, "width": width, "height": height}


def _optional_region(
    x: int | None, y: int | None, width: int | None, height: int | None
) -> tuple[int, int, int, int] | None:
    if x is None and y is None and width is None and height is None:
        return None
    if x is None or y is None or width is None or height is None:
        raise ValueError("x, y, width and height must be supplied together")
    return (x, y, width, height)


def build_server(config: VisualMCPConfig | None = None) -> MCPServer:
    client = VisualAPIClient(config or VisualMCPConfig.from_env())
    server = MCPServer(
        "smaller-gnsis",
        description=(
            "Rolling visual understanding and grounded decisions over a host-owned "
            "live screen stream. Use a session-scoped planner token. The host alone "
            "captures and executes actions."
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
    def visual_perceive(
        request_id: str,
        focus: str | None = None,
        target_x: int | None = None,
        target_y: int | None = None,
    ) -> dict[str, object]:
        """Describe the current visible screen and recent visible changes.

        Optionally focus the description on one visible question, and/or supply
        a viewport pixel (target_x, target_y) to ground: the response then
        includes what is visibly at that point with a validated box. This does
        not require a task. Reuse the same request_id when retrying.
        """

        if (target_x is None) != (target_y is None):
            raise ValueError("target_x and target_y must be supplied together")
        target = None if target_x is None or target_y is None else (target_x, target_y)
        return client.perceive(request_id, focus, target)

    @server.tool()
    def visual_state() -> dict[str, object]:
        """Inspect task, bounded history, current frame and usage state."""

        return client.state()

    @server.tool()
    def visual_history(limit: int | None = None) -> dict[str, object]:
        """List retained frames and earlier perception answers, newest first.

        Frames listed here can be addressed by frame_id in visual_inspect and
        visual_read_pixels; older frames have left the bounded window.
        """

        return client.history(limit)

    @server.tool()
    def visual_inspect(
        question: str,
        frame_id: str | None = None,
        x: int | None = None,
        y: int | None = None,
        width: int | None = None,
        height: int | None = None,
        display_size: int | None = None,
    ) -> list[TextContent | Image]:
        """Look closely at a retained frame: the region x, y, width, height
        (viewport pixels) cropped at full resolution and enlarged to about
        display_size pixels. Omit the region for the whole frame and frame_id
        for the latest frame. State in question what you are checking.
        """

        view = client.inspect(
            frame_id, _optional_region(x, y, width, height), display_size
        )
        image = view.pop("image")
        if not isinstance(image, dict):
            raise RuntimeError("Smaller GNSIS API returned no inspection image")
        view["question"] = " ".join(question.split())[:500]
        return [
            TextContent(type="text", text=json.dumps(view)),
            Image(data=base64.b64decode(str(image["data"])), format="png"),
        ]

    @server.tool()
    def visual_read_pixels(
        x: int,
        y: int,
        width: int,
        height: int,
        frame_id: str | None = None,
        step: int = 1,
    ) -> dict[str, object]:
        """Exact #rrggbb colours of a retained frame's region, sampled every
        step pixels (at most 4096 samples). frame_id defaults to the latest frame.
        """

        return client.read_pixels((x, y, width, height), frame_id, step)

    @server.tool()
    def visual_reset() -> dict[str, object]:
        """Clear the current goal and visual action history."""

        return client.reset()

    return server


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
