from __future__ import annotations

import socket

import uvicorn

from gnsis_runtime.visual.api import VisualAPISettings, create_visual_api
from gnsis_runtime.visual.schema import Decision, Target
from gnsis_runtime.visual.service import VisualService


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


class ReadyServer(uvicorn.Server):
    async def startup(self, sockets=None) -> None:
        await super().startup(sockets=sockets)
        if self.started:
            print(f"READY {self.config.port}", flush=True)


def main() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    app = create_visual_api(
        VisualService(FixedPolicy()),
        VisualAPISettings(bearer_token="sdk-test-token"),
    )
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
