from __future__ import annotations

import socket

import uvicorn

from gnsis_runtime.visual.perception import PerceivedElement, VisualPerception
from gnsis_runtime.visual.schema import Decision, Target
from gnsis_runtime.visual.serve import build_app


class FixedPolicy:
    name = "fixed"

    def decide(
        self,
        frame,
        goal,
        history,
        motion,
        viewport,
        cache,
        allowed_actions=None,
    ):
        return Decision(
            "click",
            0.9,
            Target(10, 10),
            frame_id=frame.frame_id,
        )

    def perceive(self, frames, motion, viewport):
        return VisualPerception(
            summary="A red test frame is visible.",
            visible_text=(),
            elements=(
                PerceivedElement(
                    "Test frame",
                    "image",
                    "",
                    (0, 0, viewport[0], viewport[1]),
                    "visible",
                    1.0,
                ),
            ),
            changes=(),
            confidence=1.0,
            frame_id=str(frames[-1].frame_id),
            observed_frame_ids=tuple(str(frame.frame_id) for frame in frames),
            motion=motion,
            viewport=viewport,
        )


class ReadyServer(uvicorn.Server):
    async def startup(self, sockets=None) -> None:
        await super().startup(sockets=sockets)
        if self.started:
            print(f"READY {self.config.port}", flush=True)


def main() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    app = build_app(FixedPolicy(), "sdk-test-host-token")
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )
    ReadyServer(config).run()


if __name__ == "__main__":
    main()
