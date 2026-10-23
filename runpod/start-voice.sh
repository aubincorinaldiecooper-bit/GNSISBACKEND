#!/usr/bin/env bash
# Start the GNSIS voice runtime on a RunPod pod.
#
# Refuses to start, with a message that names the fix, when the pod is not set
# up the way the runtime needs: the edge secret, the weights on the network
# volume at /models, two GPUs, and complete memory credentials.
set -euo pipefail

CONFIG="${GNSIS_CONFIG:-/workspace/runtime/configs/gnsis-voice.yaml}"

fail() {
  echo "start-voice: $*" >&2
  exit 1
}

# A pod's address is public. Without the secret every route would be open, so
# do not start at all; EdgeGuard enforces it on every route but GET /health.
[ -n "${GNSIS_EDGE_SECRET:-}" ] \
  || fail "GNSIS_EDGE_SECRET is not set. Use the same value as the site's GNSIS_EDGE_SECRET."
export GNSIS_EDGE_ALL_ROUTES="${GNSIS_EDGE_ALL_ROUTES:-1}"

# The same assets modal/gnsis_voice.py checks in cache_gnsis_models.
for path in \
  /models/MiniCPM-o-4_5 \
  /models/GNSIS/thinker \
  /models/Gander/talker/model.safetensors \
  /models/Gander/talker/talker_config.json \
  /models/Gander/talker/assets/token2wav \
  /models/Gander/talker/assets/ref_audio.wav \
  /opt/gnsis-asr/faster-whisper-small.en/model.bin; do
  [ -e "$path" ] || fail "missing $path. Is the network volume with the weights mounted at /models?"
done

# The Thinker runs on GPU 0 and the detached Talker on GPU 1.
gpus="$(python -c 'import torch; print(torch.cuda.device_count())')"
[ "$gpus" -ge 2 ] || fail "gnsis-voice.yaml needs 2 GPUs (Thinker + Talker); this pod has $gpus."

# Memory: required whenever a SimpleMem URL is configured, as on Modal.
if [ -n "${GNSIS_SIMPLEMEM_URL:-}" ]; then
  token="${GNSIS_SIMPLEMEM_TOKEN:-${GNSIS_SIMPLEMEM_INTERNAL_TOKEN:-}}"
  [ -n "$token" ] || fail "GNSIS_SIMPLEMEM_URL is set but GNSIS_SIMPLEMEM_TOKEN is not."
  [ -n "${GNSIS_DESKTOP_GRANT_PUBLIC_KEY:-}" ] \
    || fail "GNSIS_DESKTOP_GRANT_PUBLIC_KEY is required with memory on: durable memory is refused without it."
  export GNSIS_SIMPLEMEM_TOKEN="$token" GNSIS_SIMPLEMEM_INTERNAL_TOKEN="$token"
  export GNSIS_SIMPLEMEM_REQUIRED="${GNSIS_SIMPLEMEM_REQUIRED:-true}"
else
  echo "start-voice: GNSIS_SIMPLEMEM_URL is not set; live sessions run without durable memory." >&2
  export GNSIS_SIMPLEMEM_REQUIRED="${GNSIS_SIMPLEMEM_REQUIRED:-false}"
fi
export GNSIS_DESKTOP_GRANT_ISSUER="${GNSIS_DESKTOP_GRANT_ISSUER:-gnsis-control-plane}"

cd /workspace
exec gnsis-serve --config "$CONFIG"
