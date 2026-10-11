"""Experimental GNSIS runtime with Dots replacing Talker + Token2wav.

The production `gnsis-voice` app remains unchanged. This separately named
benchmark app keeps the Gander Thinker on GPU 0 and runs stock Dots double
streaming in a dependency-isolated process on GPU 1. The adapter preserves the
existing desktop wire format, output epochs, cancellation, and playback ACKs.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import modal

APP_NAME = "gnsis-dots-voice"
PORT = 7975
DOTS_PORT = 7976
CONFIG_PATH = "/workspace/runtime/configs/gnsis-dots-voice.yaml"
DOTS_MODEL_PATH = "/models/Dots/dots.tts-mf-2steps-stts"
DOTS_PROMPT_AUDIO = "/models/Gander/talker/assets/ref_audio.wav"
DOTS_REVISION = "b65ca7149fb43b8fb8d52916aa12738deda7b188"

# The directory this repository keeps the realtime source in, relative to its
# root. Modal resolves it lazily, at deploy time and not at import, so a wrong
# name here would build a perfectly valid image with no source in it and fail
# only once someone deployed. Checked on import instead, so the CI import of
# this file catches a move of the source tree.
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

# The model volume holds MiniCPM-o, the GNSIS Thinker, the existing reference
# voice, and the experimental Dots checkpoint.
MODELS_VOLUME_NAME = os.environ.get("GNSIS_MODELS_VOLUME") or "gnsis-model-weights"

models = modal.Volume.from_name(MODELS_VOLUME_NAME, create_if_missing=False)

# The shared secret the site's proxy stamps on every socket it forwards. The
# runtime refuses the two live sockets (/ws/duplex, /ws/screen) without it, so
# reaching this app's public .modal.run address directly — going around the
# site, and around everything applied there — cannot open a session on the
# model. It does not stop the GPUs waking: plain requests such as /health are
# not checked, and any request that reaches the address starts the container.
#
# from_dict passes the value to containers as an environment variable at run
# time; it is never written into an image layer. It is read from whoever runs
# the deploy. The worker's deploy passes its own GNSIS_EDGE_SECRET and refuses
# to run without one, so a worker deploy cannot switch the check off.
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
        # Trusted spoken-turn transcription for the desktop action policy.
        "faster-whisper>=1.2,<2",
        "huggingface-hub>=0.30,<1",
        # Token2wav deps. torchaudio must match the torch pin: the
        # unbounded torchaudio wheel resolves to the cu13 build and fails
        # to load libcudart.so.13 under torch 2.6 (cu124).
        "minicpmo-utils[tts]>=1.0.6,<2",
        "torchaudio==2.6.0",
    )
    .add_local_dir(SOURCE_DIR, remote_path="/workspace/runtime", copy=True)
    .run_commands(
        "python -m pip install --no-deps /workspace/runtime/minicpm_ft",
        "python -m pip install --no-deps /workspace/runtime/gnsis_runtime",
        "mkdir -p /workspace /var/gnsis /var/gnsis/ledger /opt/gnsis-asr",
        (
            'python -c "from huggingface_hub import snapshot_download; '
            "snapshot_download('Systran/faster-whisper-small.en', "
            "local_dir='/opt/gnsis-asr/faster-whisper-small.en')\""
        ),
        "python -m venv /opt/dots-venv",
        "/opt/dots-venv/bin/python -m pip install --upgrade pip",
        (
            "/opt/dots-venv/bin/python -m pip install "
            "'torch==2.8.0' 'torchaudio==2.8.0' 'transformers==4.57.1' "
            "'fastapi>=0.110' 'uvicorn[standard]>=0.29' "
            f"'git+https://github.com/rednote-hilab/dots.tts.git@{DOTS_REVISION}'"
        ),
    )
)

# include_source=True: Modal ships this file into the container and imports it
# there to find gnsis_server. With it off, every container died at that import
# with "ModuleNotFoundError: No module named 'gnsis'" — before gnsis-serve, the
# config or the checkpoint were ever touched. modal/ is a bare directory, not a
# package, so the only thing shipped is this file.
app = modal.App(APP_NAME, image=image, include_source=True)


@app.function(volumes={"/models": models}, timeout=3600)
def cache_gnsis_models() -> dict[str, str]:
    """Cache Dots and validate every asset the benchmark runtime loads."""
    from pathlib import Path

    from huggingface_hub import snapshot_download

    dots_model = Path(DOTS_MODEL_PATH)
    dots_config = dots_model / "config.json"
    if not dots_config.is_file():
        snapshot_download(
            "dots-studio/dots.tts-mf-2steps-stts",
            local_dir=dots_model,
        )
        models.commit()

    required = {
        "model": Path("/models/MiniCPM-o-4_5"),
        "thinker": Path("/models/GNSIS/thinker"),
        "dots": dots_config,
        "ref_audio": Path(DOTS_PROMPT_AUDIO),
        "asr": Path("/opt/gnsis-asr/faster-whisper-small.en/model.bin"),
    }
    for label, path in required.items():
        if not path.exists():
            raise FileNotFoundError(f"{label} not found: {path}")
    return {label: str(path) for label, path in required.items()}


# Exactly one GPU container.
#
# A live session is two WebSockets that share state — /ws/duplex creates the
# session in process-local runtime.sessions and /ws/screen looks it up there.
# With more than one container Modal can route the two sockets to different
# processes, and /ws/screen 500s on a session it cannot see: the phone sits on
# "Waiting for the first frame…" forever. Sticky routing is not a fix —
# correctness cannot depend on which backend a load balancer happens to pick.
# One container keeps the state and both sockets in one process; multi-user
# routing is designed after a single session is proven end to end.
#
# This is a literal 1 on the decorator, not an env default: a deployment that
# still sets GNSIS_MAX_CONTAINERS=5 would override a default and split the
# channels again.
@app.function(
    gpu="L40S:2",
    volumes={"/models": models},
    secrets=[edge_secret],
    # The session ceiling that actually bounds cost is the runtime's own
    # (OnlineDuplexSettings.max_session_sec), which is enforced where a session
    # is a known thing and can be tested. What this parameter bounds for a
    # web_server is not something the deploy can verify, and guessing at it
    # would risk cutting a live session short to no benefit.
    timeout=24 * 60 * 60,
    # Scale to zero when idle now that benchmark testing is complete.
    # max_containers=1 still preserves the single-process socket routing
    # requirement; scaledown_window keeps a short reuse window after activity.
    min_containers=0,
    scaledown_window=300,
    max_containers=1,
)
# One long-lived /ws/duplex must not hold the container's only input slot, or
# /ws/screen — and health traffic — would queue behind it forever. Four is
# enough for duplex + screen + a control request + headroom; this is session
# plumbing, not throughput.
@modal.concurrent(max_inputs=4)
@modal.web_server(PORT, startup_timeout=1800, requires_proxy_auth=True)
def gnsis_server() -> None:
    """Serve the unchanged Thinker with Dots isolated on the second GPU."""
    import time
    import urllib.request

    dots_env = os.environ.copy()
    dots_env["CUDA_VISIBLE_DEVICES"] = "1"
    dots_env["TORCHINDUCTOR_CACHE_DIR"] = "/models/Dots/inductor-cache"
    subprocess.Popen(
        [
            "/opt/dots-venv/bin/python",
            "/workspace/runtime/gnsis_runtime/gnsis_runtime/dots_sidecar.py",
            "--port",
            str(DOTS_PORT),
            "--model",
            DOTS_MODEL_PATH,
            "--prompt-audio",
            DOTS_PROMPT_AUDIO,
        ],
        cwd="/workspace",
        env=dots_env,
    )
    deadline = time.monotonic() + 1_200
    while True:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{DOTS_PORT}/health",
                timeout=2,
            ):
                break
        except Exception:
            if time.monotonic() >= deadline:
                raise RuntimeError("Dots renderer did not become ready")
            time.sleep(2)

    env = os.environ.copy()
    subprocess.Popen(
        ["gnsis-serve", "--config", CONFIG_PATH],
        cwd="/workspace",
        env=env,
    )
