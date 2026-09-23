"""Modal definition for the isolated GNSIS voice (Path B) realtime runtime.

Same live surface as `modal/gnsis.py`, served as a separate `gnsis-voice`
app so the production `gnsis-live` deployment is untouched while the
full-duplex path is acceptance-tested. Differences from gnsis-live:

- two L40S GPUs: GPU 0 runs the Thinker, GPU 1 the detached Talker +
  Token2wav (the runtime splits devices itself from
  `server.cuda_visible_devices` + `duplex.detached_talker_device` — config
  validation refuses same-device Thinker/Talker);
- `minicpmo-utils[tts]` in the image (Token2wav dependencies);
- `gnsis-voice.yaml` config: GNSIS Thinker + verified Gander Talker pair;
- `cache_gnsis_models` also asserts the Talker checkpoint, its bundled
  Token2wav assets, and the reference WAV — all on the model volume.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import modal

APP_NAME = "gnsis-voice"
PORT = 7975
CONFIG_PATH = "/workspace/runtime/configs/gnsis-voice.yaml"

# See modal/gnsis.py: checked only where the repository exists; inside the
# container this file sits alone at /root.
SOURCE_DIR = "runtime"
_source = Path(__file__).resolve().parent.parent / SOURCE_DIR
if modal.is_local() and not _source.is_dir():
    raise RuntimeError(
        f"{SOURCE_DIR}/ is missing from the repository root ({_source}); "
        "the realtime source tree has moved and this definition would package nothing"
    )

MODELS_VOLUME_NAME = os.environ.get("GNSIS_MODELS_VOLUME") or "gnsis-model-weights"

models = modal.Volume.from_name(MODELS_VOLUME_NAME, create_if_missing=False)

# One GPU container exactly — the same two-socket/process-local
# runtime.sessions constraint as gnsis-live. See modal/gnsis.py.
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
        "minicpmo-utils[tts]>=1.0.6,<2",
    )
    .add_local_dir(SOURCE_DIR, remote_path="/workspace/runtime", copy=True)
    .run_commands(
        "python -m pip install --no-deps /workspace/runtime/minicpm_ft",
        "python -m pip install --no-deps /workspace/runtime/gnsis_runtime",
        "mkdir -p /workspace /var/gnsis /var/gnsis/ledger",
    )
)

app = modal.App(APP_NAME, image=image, include_source=True)


@app.function(volumes={"/models": models}, timeout=600)
def cache_gnsis_models() -> dict[str, str]:
    """Validate that the model volume holds the voice runtime's assets."""
    from pathlib import Path

    required = {
        "model": Path("/models/MiniCPM-o-4_5"),
        "thinker": Path("/models/GNSIS/thinker"),
        "talker": Path("/models/Gander/talker/model.safetensors"),
        "talker_config": Path("/models/Gander/talker/talker_config.json"),
        "token2wav": Path("/models/Gander/talker/assets/token2wav"),
        "ref_audio": Path("/models/Gander/talker/assets/ref_audio.wav"),
    }
    for label, path in required.items():
        if not path.exists():
            raise FileNotFoundError(f"{label} not found: {path}")
    return {label: str(path) for label, path in required.items()}


@app.function(
    gpu="L40S:2",
    volumes={"/models": models},
    secrets=[edge_secret],
    # See modal/gnsis.py: the session ceiling belongs to the runtime's own
    # max_session_sec, not to this opaque web-server timeout.
    timeout=24 * 60 * 60,
    scaledown_window=60,
    max_containers=1,
)
# One long-lived /ws/duplex must not hold the container's only input slot.
# See modal/gnsis.py.
@modal.concurrent(max_inputs=4)
@modal.web_server(PORT, startup_timeout=1800)
def gnsis_server() -> None:
    """Serve the isolated voice runtime from the source under runtime/."""
    env = os.environ.copy()
    env.setdefault("CUDA_VISIBLE_DEVICES", "0")
    subprocess.Popen(
        ["gnsis-serve", "--config", CONFIG_PATH],
        cwd="/workspace",
        env=env,
    )
