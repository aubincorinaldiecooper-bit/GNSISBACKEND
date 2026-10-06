"""Modal definition for the Smaller GNSIS visual decision service.

Serves `gnsis_runtime.visual.serve` — the authenticated frame-stream +
bounded-decision API that host connectors (browser Hub, desktop Host) call.
The JEV engine runs on one GPU: a frozen MiniCPM-V 4.6 backbone (from the HF
cache volume) plus the JEV decision head checkpoint on `gnsis-visual-data`.
Florence-2 (focused target grounding/OCR) shares the same container and GPU; it
loads lazily on the first target request, so idle cold starts pay nothing for
it. The Qwen3-VL fallback stays off unless GNSIS_VISUAL_FALLBACK_GROUNDER=qwen.

One container on purpose: VisualService keeps sessions, frame buffers and
replay state in process memory, so every request for a session must land in
the same process — the same single-process routing rule as gnsis-voice's
duplex/screen socket pair.

Auth is the service's own host token (or grant verifier), checked inside the
FastAPI app, so the public .modal.run address is unusable without it. The
token is read from the deployer's GNSIS_VISUAL_HOST_TOKEN and passed to the
container as a Secret — never baked into an image layer.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import modal

APP_NAME = "gnsis-visual"
PORT = 8790

# See gnsis_voice.py for why this import-time check exists: Modal re-imports
# this file inside the container, where only the baked image paths are real.
SOURCE_DIR = "runtime"
_source = Path(__file__).resolve().parent.parent / SOURCE_DIR
if modal.is_local() and not _source.is_dir():
    raise RuntimeError(
        f"{SOURCE_DIR}/ is missing from the repository root ({_source}); "
        "the realtime source tree has moved and this definition would package nothing"
    )

VISUAL_VOLUME_NAME = os.environ.get("GNSIS_VISUAL_DATA_VOLUME") or "gnsis-visual-data"
HF_CACHE_VOLUME_NAME = os.environ.get("MINICPM_V46_CACHE_VOLUME") or "minicpm-v46-cache"

visual_data = modal.Volume.from_name(VISUAL_VOLUME_NAME, create_if_missing=False)
hf_cache = modal.Volume.from_name(HF_CACHE_VOLUME_NAME, create_if_missing=False)

# The JEV head checkpoint the benchmark trained (896px scale, 16x downsample —
# the BackboneConfig defaults; the .json sidecar records the training config).
JEV_HEAD_PATH = "/visual/heads/jev-896-16x.pt"
BACKBONE_REPO = "openbmb/MiniCPM-V-4.6"
GROUNDER_REPO = "florence-community/Florence-2-large-ft"
GROUNDER_REVISION = "26b734a54fdfbf9c398351eedfabb7f27fc470b7"
# Container seconds are billed whether or not a request is in flight; one
# minute of warm idle covers a connector's inter-request gaps without paying
# for five.
SCALEDOWN_WINDOW_SEC = 60
# L40S is the measured production tier; override (e.g. GNSIS_VISUAL_GPU=L4) only
# for a cost/latency measurement run, never silently in a deploy.
GPU = os.environ.get("GNSIS_VISUAL_GPU") or "L40S"

host_secret = modal.Secret.from_dict(
    {"GNSIS_VISUAL_HOST_TOKEN": os.environ.get("GNSIS_VISUAL_HOST_TOKEN", "")}
)

image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("libgl1", "libglib2.0-0")
    .uv_pip_install(
        # The [visual] extra pins from runtime/gnsis_runtime/pyproject.toml.
        "torch==2.13.0",
        "torchvision==0.28.0",
        "transformers==5.16.1",
        "accelerate==1.14.0",
        "huggingface_hub==1.33.0",
        "numpy==2.5.2",
        "rapidocr==3.9.2",
        "onnxruntime==1.30.0",
        "opencv-python==5.0.0.93",
        "xgrammar==0.2.8",
        "fastapi>=0.110",
        "uvicorn[standard]>=0.29",
        "websockets>=12",
        "pillow>=10",
        "PyJWT[crypto]>=2.8",
    )
    .add_local_dir(SOURCE_DIR, remote_path="/workspace/runtime", copy=True)
    # --no-deps: gnsis_runtime's declared deps include mcpmft's transformers
    # 4.51 pin, which conflicts with the visual stack and is not imported by
    # the visual serve path.
    .run_commands("python -m pip install --no-deps /workspace/runtime/gnsis_runtime")
)

app = modal.App(APP_NAME, image=image, include_source=True)


@app.function(volumes={"/visual": visual_data, "/hf-cache": hf_cache}, timeout=600)
def cache_gnsis_visual() -> dict[str, str]:
    """Validate the volumes hold the assets this service loads; cache Florence-2."""
    from pathlib import Path

    from huggingface_hub import snapshot_download

    grounder_path = snapshot_download(
        GROUNDER_REPO,
        revision=GROUNDER_REVISION,
        cache_dir="/hf-cache/hub",
    )
    hf_cache.commit()
    required = {
        "jev_head": Path(JEV_HEAD_PATH),
        "jev_head_config": Path("/visual/heads/jev-896-16x.json"),
        "backbone_cache": Path("/hf-cache/hub/models--openbmb--MiniCPM-V-4.6"),
        "grounder_cache": Path(grounder_path),
    }
    for label, path in required.items():
        if not path.exists():
            raise FileNotFoundError(f"{label} not found: {path}")
    return {label: str(path) for label, path in required.items()}


def _serve() -> subprocess.Popen[bytes]:
    env = os.environ.copy()
    env.setdefault("CUDA_VISIBLE_DEVICES", "0")
    env["HF_HUB_CACHE"] = "/hf-cache/hub"
    env.setdefault("GNSIS_VISUAL_GROUNDER", "florence")
    env.setdefault("GNSIS_VISUAL_FALLBACK_GROUNDER", "none")
    return subprocess.Popen(
        [
            "smaller-gnsis-serve",
            "--model",
            BACKBONE_REPO,
            "--head",
            JEV_HEAD_PATH,
            "--device",
            "cuda",
            "--dtype",
            "bfloat16",
            "--host",
            "0.0.0.0",
            "--port",
            str(PORT),
        ],
        cwd="/workspace",
        env=env,
    )


@app.function(
    gpu=GPU,
    volumes={"/visual": visual_data, "/hf-cache": hf_cache},
    secrets=[host_secret],
    timeout=24 * 60 * 60,
    min_containers=0,
    scaledown_window=SCALEDOWN_WINDOW_SEC,
    max_containers=1,
)
# Session state is process-local, so all of a session's requests must reach
# one container — max_containers=1 above, not a tunable.
@modal.concurrent(max_inputs=8)
# The `main` environment blocks unauthenticated web endpoints; this app is
# meant to sit behind the same proxy path as gnsis-voice, which stamps the
# proxy-auth headers it needs. For direct connector access use visual_tunnel.
@modal.web_server(PORT, startup_timeout=1800, requires_proxy_auth=True)
def gnsis_visual_server() -> None:
    _serve()


@app.function(
    gpu=GPU,
    volumes={"/visual": visual_data, "/hf-cache": hf_cache},
    secrets=[host_secret],
    timeout=24 * 60 * 60,
    max_containers=1,
)
async def visual_tunnel() -> str:
    """Serve the visual API behind a Modal TLS tunnel, for direct host
    connectors in environments where unauthenticated proxy URLs are blocked.

    The tunnel URL is public but the API still refuses everything without the
    deployed GNSIS_VISUAL_HOST_TOKEN.
    """
    import asyncio

    proc = _serve()
    async with modal.forward(PORT) as tunnel:
        print(f"GNSIS_VISUAL_BASE_URL={tunnel.url}", flush=True)
        try:
            while proc.poll() is None:
                await asyncio.sleep(5)
        finally:
            proc.terminate()
        return tunnel.url
