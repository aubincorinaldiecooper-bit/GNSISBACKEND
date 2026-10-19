"""Modal definition for the OmniJev developer decide service (beta).

Serves `gnsis_runtime.local_controller_api` — POST /v1/decide returning the
typed verdict (candidate + confidence + abstain) that beta developers call.
This is the same boundary the desktop uses locally; here it is a hosted API.

Runs the stable reference path (MSO_FLA=0): measured 1.9-4.1s/decide on L4,
7/7 clean calls. The fla fast path (~0.26s) is disabled on purpose until the
upstream fused_recurrent IMA is resolved — correctness before speed for beta.

Auth: Bearer token checked inside the FastAPI app, so the public .modal.run
URL is unusable without a developer key. Keys come from the deployer's
GNSIS_OMNIJEV_API_KEYS (comma-separated, one per developer) as a Secret —
never baked into the image.
"""

from __future__ import annotations

import os
from pathlib import Path

import modal

APP_NAME = "omnijev-decide"

SOURCE_DIR = "runtime"
_source = Path(__file__).resolve().parent.parent / SOURCE_DIR
if modal.is_local() and not _source.is_dir():
    raise RuntimeError(
        f"{SOURCE_DIR}/ is missing from the repository root ({_source}); "
        "the runtime tree has moved and this definition would package nothing"
    )

# Same HF volume the bench runs populate; hub/xet caches are read-mostly here.
hf = modal.Volume.from_name("gnsis-omnijev-bench-hf", create_if_missing=False)

OMNIJEV_COMMIT = "14dbec4f71e194852c8d7b88ab36ef639493f400"
OMNIJEV_CKPT_REPO = "tinnel123/OmniJev-0.8B"
BASE_MODEL_REPO = "Qwen/Qwen3.5-0.8B"

# L4 is the benchmarked tier for this model. Beta serves cold-to-zero:
# min_containers=0 means idle time costs nothing; a cold start re-warms.
GPU = os.environ.get("GNSIS_OMNIJEV_GPU") or "L4"
SCALEDOWN_WINDOW_SEC = 300

api_keys_secret = modal.Secret.from_dict(
    {"GNSIS_OMNIJEV_API_KEYS": os.environ.get("GNSIS_OMNIJEV_API_KEYS", "")}
)

image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("git")
    .pip_install(
        # Same pins as the benchmarked stack.
        "torch==2.14.1",
        "torchvision==0.29.1",
        "transformers==5.20.1",
        "peft==0.21.2",
        "accelerate==1.15.0",
        "safetensors==0.8.0",
        "pillow==13.0.0",
        "huggingface_hub",
        "fastapi>=0.110",
        "uvicorn[standard]>=0.29",
    )
    .run_commands(
        "git clone https://github.com/tinnel123666888/OmniJev /root/omnijev-source "
        f"&& cd /root/omnijev-source && git checkout {OMNIJEV_COMMIT}"
    )
    .add_local_dir(SOURCE_DIR, remote_path="/workspace/runtime", copy=True)
    # --no-deps: gnsis_runtime's declared deps conflict with this stack and are
    # not imported by the decide path.
    .run_commands("python -m pip install --no-deps /workspace/runtime/gnsis_runtime")
)

app = modal.App(APP_NAME, image=image, include_source=True)


@app.function(
    gpu=GPU,
    volumes={"/hf": hf},
    secrets=[api_keys_secret],
    timeout=24 * 60 * 60,
    min_containers=0,
    scaledown_window=SCALEDOWN_WINDOW_SEC,
    max_containers=1,
)
# One container on purpose: the engine holds model state in process memory and
# decides are serialized on the GPU anyway.
# The `main` environment blocks unauthenticated URLs (same rule as gnsis-voice):
# the proxy-auth layer sits in front of our Bearer keys until the workspace
# allows a public URL or the site proxy fronts this path for beta developers.
@modal.asgi_app(requires_proxy_auth=True)
def decide_service():
    import logging
    import sys

    os.environ["HF_HOME"] = "/hf"
    # uvicorn's handlers filter at the handler level, but the library logger
    # inherits WARNING; decide attribution lines are INFO.
    logging.getLogger("gnsis_runtime.local_controller_api").setLevel(logging.INFO)
    # Stable reference path for beta — fla fast path rejoins after the
    # upstream IMA is fixed.
    os.environ["MSO_FLA"] = "0"
    sys.path.insert(0, "/root/omnijev-source")

    from huggingface_hub import snapshot_download

    os.environ["GNSIS_OMNIJEV_SOURCE"] = "/root/omnijev-source"
    os.environ["GNSIS_OMNIJEV_CHECKPOINT"] = snapshot_download(OMNIJEV_CKPT_REPO)
    os.environ["GNSIS_QWEN_BASE_MODEL"] = snapshot_download(BASE_MODEL_REPO)

    keys = [
        key.strip()
        for key in os.environ.get("GNSIS_OMNIJEV_API_KEYS", "").split(",")
        if key.strip()
    ]
    if not keys:
        raise RuntimeError("GNSIS_OMNIJEV_API_KEYS must name at least one developer key")

    from gnsis_runtime.local_controller import LocalDecisionEngine
    from gnsis_runtime.local_controller_api import build_local_controller_app
    from gnsis_runtime.omnijev_local import backend_from_env

    engine = LocalDecisionEngine(backend_from_env())
    return build_local_controller_app(engine, keys[0], extra_tokens=keys[1:])
