"""Modal definition for the GNSIS realtime multimodal runtime.

This app serves the GNSIS live surface and realtime model from the source under
`runtime/`. Model artifacts are supplied through the configured Modal volume;
the MVP itself has no external action-worker dependency.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import modal

APP_NAME = "gnsis-live"
PORT = 7975
CONFIG_PATH = "/workspace/runtime/configs/gnsis-live.yaml"

# The directory this repository keeps the realtime source in, relative to its
# root. Modal resolves it lazily, at deploy time and not at import, so a wrong
# name here used to build a perfectly valid image with no source in it and fail
# only once someone deployed. Checked on import instead, so the CI import of
# this file catches a move of the source tree the way it caught nothing before.
SOURCE_DIR = "runtime"
_source = Path(__file__).resolve().parent.parent / SOURCE_DIR
if not _source.is_dir():
    raise RuntimeError(
        f"{SOURCE_DIR}/ is missing from the repository root ({_source}); "
        "the realtime source tree has moved and this definition would package nothing"
    )

# The existing model volume holds MiniCPM-o and the GNSIS Thinker checkpoint.
# A wrong name fails the deploy rather than creating a fresh empty volume.
MODELS_VOLUME_NAME = os.environ.get("GNSIS_MODELS_VOLUME") or "gnsis-model-weights"

models = modal.Volume.from_name(MODELS_VOLUME_NAME, create_if_missing=False)

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install(
        "build-essential",
        "ffmpeg",
        "git",
        "libgl1",
        "libglib2.0-0",
        "libsndfile1",
    )
    .uv_pip_install(
        "torch==2.6.0",
        "transformers==4.51.0",
        "accelerate>=1.10,<2",
        "av>=14,<18",
        "deepspeed>=0.19,<0.20",
        "librosa>=0.9,<0.11",
        "numpy>=1.26,<2",
        "pillow>=10",
        "pyarrow>=15",
        "pyyaml>=6",
        "safetensors>=0.4",
        "scipy>=1.11",
        "soundfile>=0.12",
        "tqdm>=4.66",
        "fastapi>=0.110",
        "uvicorn[standard]>=0.29",
        "websockets>=12",
    )
    # The realtime source tree, which lives at runtime/ in this repository and
    # is served from /workspace/runtime inside the image.
    .add_local_dir(SOURCE_DIR, remote_path="/workspace/runtime", copy=True)
    .run_commands(
        "python -m pip install --no-deps /workspace/runtime/minicpm_ft",
        "python -m pip install --no-deps /workspace/runtime/gnsis_runtime",
        "mkdir -p /workspace /var/gnsis /var/gnsis/ledger",
    )
)

app = modal.App(APP_NAME, image=image, include_source=False)


@app.function(volumes={"/models": models}, timeout=600)
def cache_gnsis_models() -> dict[str, str]:
    """Validate that the externally supplied model volume is populated."""
    from pathlib import Path

    model_dir = Path("/models/MiniCPM-o-4_5")
    thinker_checkpoint = Path("/models/GNSIS/thinker")
    if not model_dir.is_dir():
        raise FileNotFoundError(f"MiniCPM model directory not found: {model_dir}")
    if not thinker_checkpoint.exists():
        raise FileNotFoundError(f"GNSIS Thinker checkpoint not found: {thinker_checkpoint}")
    return {"model": str(model_dir), "thinker": str(thinker_checkpoint)}


@app.function(
    gpu="L40S",
    volumes={"/models": models},
    timeout=24 * 60 * 60,
    scaledown_window=60,
)
@modal.web_server(PORT, startup_timeout=1800)
def gnsis_server() -> None:
    """Serve the GNSIS realtime runtime from the source under runtime/."""
    env = os.environ.copy()
    env.setdefault("CUDA_VISIBLE_DEVICES", "0")
    subprocess.Popen(
        ["gnsis-serve", "--config", CONFIG_PATH],
        cwd="/workspace",
        env=env,
    )
