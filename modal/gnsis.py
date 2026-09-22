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
#
# Only checked where the repository exists. Modal imports this same file a
# second time, inside the container, to find gnsis_server — and there it sits
# alone at /root with no repository around it; the source is already baked into
# the image at /workspace/runtime. Run there, this check would fail every
# container start.
SOURCE_DIR = "runtime"
_source = Path(__file__).resolve().parent.parent / SOURCE_DIR
if modal.is_local() and not _source.is_dir():
    raise RuntimeError(
        f"{SOURCE_DIR}/ is missing from the repository root ({_source}); "
        "the realtime source tree has moved and this definition would package nothing"
    )

# The existing model volume holds MiniCPM-o and the GNSIS Thinker checkpoint.
# A wrong name fails the deploy rather than creating a fresh empty volume.
MODELS_VOLUME_NAME = os.environ.get("GNSIS_MODELS_VOLUME") or "gnsis-model-weights"

models = modal.Volume.from_name(MODELS_VOLUME_NAME, create_if_missing=False)

# The ceiling on how many GPUs this app will ever hold at once.
#
# The runtime serves one session per container, so concurrent sessions and
# running GPUs are the same number. The live page is deliberately open to
# anyone with the link, which means without a ceiling the only limit on the
# bill is the account's own quota: a short script opening sockets would start
# GPUs until it hit that, and every real visitor would be queued behind them.
#
# Past this many, Modal queues instead of starting another, and the runtime's
# model lock answers "busy, retry" to whoever is waiting. That is a deliberate
# trade — some visitors are turned away at a busy moment — and it is the point:
# a number we chose beats a number an attacker chooses.
MAX_CONTAINERS = int(os.environ.get("GNSIS_MAX_CONTAINERS") or 5)

# The shared secret the site's proxy stamps on every socket it forwards. The
# runtime refuses sockets without it, so reaching this app's public .modal.run
# address directly — going around the site, and around everything applied
# there — gets a close rather than a GPU.
#
# from_dict passes the value to containers as an environment variable at run
# time; it is never written into an image layer. It is read from whoever runs
# the deploy.
#
# THIS side is the switch. Unset here the runtime's check is off, it accepts
# anything, and it says so at startup — so deploying this code before the value
# exists cannot take the site down. Setting it here FIRST can: the site would
# still be sending an empty header, and every session would be refused. Set the
# site's GNSIS_EDGE_SECRET first and this one second; on a rollback, clear this
# one first and the site's last.
edge_secret = modal.Secret.from_dict(
    {"GNSIS_EDGE_SECRET": os.environ.get("GNSIS_EDGE_SECRET", "")}
)

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
        "deepspeed>=0.19,<0.21",
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

# include_source=True: Modal ships this file into the container and imports it
# there to find gnsis_server. With it off, every container died at that import
# with "ModuleNotFoundError: No module named 'gnsis'" — before gnsis-serve, the
# config or the checkpoint were ever touched. modal/ is a bare directory, not a
# package, so the only thing shipped is this file.
app = modal.App(APP_NAME, image=image, include_source=True)


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
    secrets=[edge_secret],
    # Left as it was on purpose. The session ceiling that actually bounds cost
    # is now the runtime's own (OnlineDuplexSettings.max_session_sec), which is
    # enforced where a session is a known thing and can be tested. What this
    # parameter bounds for a web_server is not something the deploy can verify,
    # and guessing at it would risk cutting a live session short to no benefit.
    timeout=24 * 60 * 60,
    scaledown_window=60,
    max_containers=MAX_CONTAINERS,
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
