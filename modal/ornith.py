"""Optional Ornith action-layer deployment for GNSIS.

The live perception MVP does not depend on this app. When enabled later, Ornith
is served through a separate OpenAI-compatible vLLM endpoint and can be selected
as a worker provider by an explicit live configuration.
"""

from __future__ import annotations

import os
import subprocess

import modal

# Kept as a separate app so perception and delegated task execution can scale
# independently.
APP_NAME = os.environ.get("ORNITH_MODAL_APP_NAME") or "gnsis-ornith"
PORT = 8000
MODEL = "ornith-ai/Ornith-1.5-9B"
SERVED_MODEL_NAME = "ornith"

# The weights cache and the secret holding ORNITH_API_KEY. Both must already
# exist: a name that does not is a failed deploy, never a new resource. A
# cache created empty by mistake would look fine and re-download 9B of weights
# on every cold start.
CACHE_VOLUME_NAME = os.environ.get("ORNITH_CACHE_VOLUME") or "gnsis-ornith-cache"
SECRET_NAME = os.environ.get("ORNITH_SECRET_NAME") or "gnsis-ornith-auth"

# Pin this to a tested digest before production use.
IMAGE_TAG = os.environ.get("ORNITH_VLLM_IMAGE") or "vllm/vllm-openai:latest"

cache = modal.Volume.from_name(CACHE_VOLUME_NAME, create_if_missing=False)
ornith_secret = modal.Secret.from_name(SECRET_NAME)

image = modal.Image.from_registry(IMAGE_TAG).entrypoint([])

app = modal.App(APP_NAME, image=image, include_source=False)


@app.function(
    gpu="L40S",
    volumes={"/root/.cache/huggingface": cache},
    secrets=[ornith_secret],
    timeout=24 * 60 * 60,
    scaledown_window=60,
)
@modal.web_server(PORT, startup_timeout=1800)
def ornith_server_v2() -> None:
    """Serve Ornith through vLLM's OpenAI-compatible API."""
    api_key = (os.environ.get("ORNITH_API_KEY") or "").strip()
    if not api_key:
        # Refuse rather than start an unauthenticated GPU endpoint.
        raise RuntimeError("ORNITH_API_KEY is required")

    # The key is passed as an argument because that is what the deployment
    # being reproduced does, and swapping it for vLLM's environment variable
    # without checking that this image reads it risks a server that starts
    # with no authentication at all. It is visible in the container's own
    # process list; the container runs nothing else.
    subprocess.Popen(
        [
            "vllm",
            "serve",
            MODEL,
            "--host",
            "0.0.0.0",
            "--port",
            str(PORT),
            "--served-model-name",
            SERVED_MODEL_NAME,
            "--dtype",
            "bfloat16",
            "--max-model-len",
            "32768",
            "--trust-remote-code",
            "--enable-prefix-caching",
            "--enable-chunked-prefill",
            "--reasoning-parser",
            "qwen3",
            "--api-key",
            api_key,
        ],
        env=os.environ.copy(),
    )
