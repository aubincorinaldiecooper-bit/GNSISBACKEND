"""Modal definition for the GNSIS live runtime: the phone page and the model behind it.

The runtime is vendored under ``gnsis/``. This app is the GNSIS-owned
successor of GNSIS's ``legacy-live-service``: the same realtime GNSIS
runtime and model volume, under a new name so the two can run side by side
until the new one is verified and the old one is shut down
(docs/live_runtime.md).

The MVP is perception-only. It does not load an external worker provider and
therefore does not require the historical Ornith secret.
"""

from __future__ import annotations

import os
import subprocess

import modal

APP_NAME = "gnsis-live"
PORT = 7975
CONFIG_PATH = "/workspace/live/configs/gnsis-live.yaml"

# The existing model volume holds MiniCPM-o and the GNSIS Thinker checkpoint.
# A wrong name fails the deploy rather than creating a fresh empty volume.
MODELS_VOLUME_NAME = os.environ.get("GNSIS_MODELS_VOLUME") or "gnsis-models"

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
    .add_local_dir("gnsis", remote_path="/workspace/live", copy=True)
    .run_commands(
        "python -m pip install --no-deps /workspace/live/minicpm_ft",
        "python -m pip install --no-deps /workspace/live/runtime",
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
def gnsis_live_server() -> None:
    """Serve the live runtime from the vendored source under gnsis/."""
    env = os.environ.copy()
    env.setdefault("CUDA_VISIBLE_DEVICES", "0")
    subprocess.Popen(
        ["gnsis-live-serve", "--config", CONFIG_PATH],
        cwd="/workspace",
        env=env,
    )
