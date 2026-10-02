from __future__ import annotations

import argparse
import os
from collections.abc import Callable, Sequence
from typing import Any

from fastapi import FastAPI

from .api import VisualAPISettings, create_visual_api
from .runtime import VisualDecisionPolicy
from .service import VisualService


def build_app(
    policy: VisualDecisionPolicy,
    host_token: str,
    *,
    max_sessions: int = 32,
    cache_factory: Callable[[], Any] | None = None,
) -> FastAPI:
    service = VisualService(
        policy,
        cache_factory=cache_factory,
        max_sessions=max_sessions,
    )
    return create_visual_api(service, VisualAPISettings(host_token=host_token))


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Serve the Smaller GNSIS visual API.")
    parser.add_argument("--model", required=True, help="MiniCPM-V backbone path")
    parser.add_argument("--head", required=True, help="JEV decision head checkpoint")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--dtype", default="float32")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8790, type=int)
    parser.add_argument("--max-sessions", default=32, type=int)
    args = parser.parse_args(argv)

    host_token = os.environ.get("GNSIS_VISUAL_HOST_TOKEN", "")
    if not host_token:
        parser.error("GNSIS_VISUAL_HOST_TOKEN must be set")

    import uvicorn

    from .backbone import BackboneConfig
    from .engine import JEVEngine, VisualCache

    engine = JEVEngine(
        BackboneConfig(model_dir=args.model, dtype=args.dtype, device=args.device),
        args.head,
    )
    app = build_app(
        engine,
        host_token,
        max_sessions=args.max_sessions,
        cache_factory=VisualCache,
    )
    uvicorn.run(app, host=args.host, port=args.port)
