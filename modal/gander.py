"""Modal definition for the GNSIS live runtime: the phone page and the model behind it.

The runtime is vendored under ``gander/``. This app is the GNSIS-owned
successor of CLIPIT's ``clipit-gander-thinker``: the same runtime, the same
model volume and the same secret, under a new name so the two can run side by
side until the new one is verified and the old one is shut down
(docs/live_runtime.md). Model weights and secrets remain external runtime
prerequisites.
"""

from __future__ import annotations

import os
import subprocess

import modal

APP_NAME = "gnsis-live"
PORT = 7975
CONFIG_PATH = "/workspace/gander/configs/gnsis-live.yaml"

# The Volume that holds the weights and the Secret that holds ORNITH_API_KEY.
# The names default to the existing resources CLIPIT #134 recorded from the
# live deployment; a deploy can override either through the environment.
# Neither call creates anything: a name that does not exist fails the deploy,
# so a wrong name cannot make a second production resource by accident.
MODELS_VOLUME_NAME = os.environ.get("GANDER_MODELS_VOLUME") or "clipit-gander-weights"
SECRET_NAME = os.environ.get("GANDER_SECRET_NAME") or "clipit-gander-ornith"

models = modal.Volume.from_name(MODELS_VOLUME_NAME, create_if_missing=False)
ornith_secret = modal.Secret.from_name(SECRET_NAME)

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
    .add_local_dir("gander", remote_path="/workspace/gander", copy=True)
    .run_commands(
        "python -m pip install --no-deps /workspace/gander/minicpm_ft",
        "python -m pip install --no-deps /workspace/gander/gander_runtime",
        "mkdir -p /workspace /var/gander /var/gander/ledger",
    )
)

app = modal.App(APP_NAME, image=image, include_source=False)


@app.function(volumes={"/models": models}, timeout=600)
def cache_gander_models() -> dict[str, str]:
    """Validate that the externally supplied model volume is populated."""
    from pathlib import Path

    model_dir = Path("/models/MiniCPM-o-4_5")
    thinker_checkpoint = Path("/models/Gander/thinker")
    if not model_dir.is_dir():
        raise FileNotFoundError(f"MiniCPM model directory not found: {model_dir}")
    if not thinker_checkpoint.exists():
        raise FileNotFoundError(f"Gander Thinker checkpoint not found: {thinker_checkpoint}")
    return {"model": str(model_dir), "thinker": str(thinker_checkpoint)}


@app.function(
    gpu="L40S",
    volumes={"/models": models},
    secrets=[ornith_secret],
    timeout=24 * 60 * 60,
    scaledown_window=60,
)
@modal.web_server(PORT, startup_timeout=1800)
def gander_server() -> None:
    """Serve the live runtime from the vendored source under gander/."""
    env = os.environ.copy()
    env.setdefault("CUDA_VISIBLE_DEVICES", "0")
    subprocess.Popen(
        ["gander-serve", "--config", CONFIG_PATH],
        cwd="/workspace",
        env=env,
    )
