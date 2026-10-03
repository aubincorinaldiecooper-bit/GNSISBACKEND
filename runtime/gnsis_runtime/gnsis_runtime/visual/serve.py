from __future__ import annotations

import argparse
import os
from collections.abc import Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from .api import (
    MAX_FRAME_BYTES,
    MAX_FRAME_HEADER_BYTES,
    VisualAPISettings,
    create_visual_api,
)
from .grants import GrantVerifier
from .grounding import (
    Florence2Grounder,
    GroundingRouter,
    Qwen3VLGrounder,
    TargetGrounder,
)
from .metering import HttpUsageSink, UsageSink
from .runtime import PanopticPolicy, VisualDecisionProvider
from .service import VisualService


def build_app(
    policy: PanopticPolicy,
    host_token: str | None = None,
    *,
    decision_provider: VisualDecisionProvider | None = None,
    grant_verifier: GrantVerifier | None = None,
    max_sessions: int = 32,
    cache_factory: Callable[[], Any] | None = None,
    usage_sink_factory: Callable[[VisualService], UsageSink] | None = None,
) -> FastAPI:
    service = VisualService(
        policy,
        decision_provider=decision_provider,
        cache_factory=cache_factory,
        max_sessions=max_sessions,
    )
    sink = usage_sink_factory(service) if usage_sink_factory is not None else None

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if sink is not None:
            sink.start(service)
        try:
            yield
        finally:
            if sink is not None:
                sink.stop()

    app = create_visual_api(
        service,
        VisualAPISettings(host_token=host_token, grant_verifier=grant_verifier),
        lifespan=lifespan,
    )
    app.state.visual_service = service
    app.state.usage_sink = sink
    return app


def build_grounder(
    primary: str,
    fallback: str,
    *,
    device: str,
    dtype: str,
) -> TargetGrounder | None:
    """Compose the focused grounding sidecar; models load on first target request."""

    if primary == "none":
        return None
    secondary: TargetGrounder | None = None
    if fallback == "qwen":
        secondary = Qwen3VLGrounder(device=device, dtype=dtype)
    return GroundingRouter(
        Florence2Grounder(device=device, dtype=dtype),
        secondary,
    )


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Serve the Smaller GNSIS visual API.")
    parser.add_argument("--model", required=True, help="MiniCPM-V backbone path")
    parser.add_argument("--head", required=True, help="JEV decision head checkpoint")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--dtype", default="float32")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8790, type=int)
    parser.add_argument("--max-sessions", default=32, type=int)
    parser.add_argument(
        "--grounder",
        default=os.environ.get("GNSIS_VISUAL_GROUNDER", "florence"),
        choices=("florence", "none"),
        help="focused target grounder loaded lazily beside MiniCPM",
    )
    parser.add_argument(
        "--fallback-grounder",
        default=os.environ.get("GNSIS_VISUAL_FALLBACK_GROUNDER", "none"),
        choices=("qwen", "none"),
        help="heavier fallback for points the primary grounder cannot resolve",
    )
    args = parser.parse_args(argv)

    host_token = os.environ.get("GNSIS_VISUAL_HOST_TOKEN") or None
    grant_public_key = os.environ.get("GNSIS_VISUAL_GRANT_PUBLIC_KEY")
    if not host_token and not grant_public_key:
        parser.error(
            "GNSIS_VISUAL_HOST_TOKEN or GNSIS_VISUAL_GRANT_PUBLIC_KEY must be set"
        )
    grant_verifier = (
        GrantVerifier(
            grant_public_key,
            issuer=os.environ.get("GNSIS_VISUAL_GRANT_ISSUER", "gnsis-control-plane"),
        )
        if grant_public_key
        else None
    )
    usage_url = os.environ.get("GNSIS_VISUAL_USAGE_URL")
    usage_secret = os.environ.get("GNSIS_VISUAL_USAGE_SECRET")
    if bool(usage_url) != bool(usage_secret):
        parser.error(
            "GNSIS_VISUAL_USAGE_URL and GNSIS_VISUAL_USAGE_SECRET must be supplied together"
        )
    if grant_public_key and not usage_url:
        parser.error(
            "grant authentication requires GNSIS_VISUAL_USAGE_URL and "
            "GNSIS_VISUAL_USAGE_SECRET so commercial usage is metered"
        )

    import uvicorn

    from .backbone import BackboneConfig
    from .engine import JEVEngine, VisualCache

    engine = JEVEngine(
        BackboneConfig(model_dir=args.model, dtype=args.dtype, device=args.device),
        args.head,
        build_grounder(
            args.grounder,
            args.fallback_grounder,
            device=args.device,
            dtype=args.dtype,
        ),
    )
    usage_sink_factory = (
        (lambda _service: HttpUsageSink(usage_url, usage_secret))
        if usage_url and usage_secret
        else None
    )
    app = build_app(
        engine,
        host_token,
        decision_provider=engine,
        grant_verifier=grant_verifier,
        max_sessions=args.max_sessions,
        cache_factory=VisualCache,
        usage_sink_factory=usage_sink_factory,
    )
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        ws_max_size=MAX_FRAME_BYTES + MAX_FRAME_HEADER_BYTES,
    )
