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

# First boot: the pod volume at /models starts empty. Pull the three model
# folders the runtime loads from the existing Modal volume, once; the pod
# volume persists across stop/start, so later boots skip this.
if [ ! -e /models/GNSIS/thinker ] || [ ! -e /models/MiniCPM-o-4_5 ] || [ ! -e /models/Gander/talker ]; then
  [ -n "${MODAL_TOKEN_ID:-}" ] && [ -n "${MODAL_TOKEN_SECRET:-}" ] \
    || fail "/models has no weights and MODAL_TOKEN_ID/MODAL_TOKEN_SECRET are not set to copy them from Modal."
  volume="${GNSIS_MODELS_VOLUME:-gnsis-model-weights}"
  echo "start-voice: copying weights from Modal volume ${volume} to /models (first boot only)..." >&2
  python -m pip install --quiet "modal==1.5.0"
  for remote in MiniCPM-o-4_5 GNSIS/thinker Gander/talker; do
    [ -e "/models/${remote}" ] && continue
    stage="/models/.staging/${remote}"
    rm -rf "$stage" && mkdir -p "$stage"
    modal volume get --env "${MODAL_ENVIRONMENT:-main}" "$volume" "$remote" "$stage" \
      || fail "copying ${remote} from Modal volume ${volume} failed."
    # Accept whichever layout the CLI wrote: the full remote path, its last
    # component, or the folder's contents directly.
    found=""
    for candidate in "$stage/$remote" "$stage/$(basename "$remote")" "$stage"; do
      if [ -d "$candidate" ] && [ -n "$(ls -A "$candidate" 2>/dev/null)" ] \
        && [ ! -d "$candidate/$(basename "$remote")" ]; then
        found="$candidate"
        break
      fi
    done
    [ -n "$found" ] || fail "downloaded ${remote} but could not find its files under ${stage}."
    mkdir -p "/models/$(dirname "$remote")"
    mv "$found" "/models/${remote}"
  done
  rm -rf /models/.staging
  echo "start-voice: weights copied." >&2
fi

# The same assets modal/gnsis_voice.py checks in cache_gnsis_models.
for path in \
  /models/MiniCPM-o-4_5 \
  /models/GNSIS/thinker \
  /models/Gander/talker/model.safetensors \
  /models/Gander/talker/talker_config.json \
  /models/Gander/talker/assets/token2wav \
  /models/Gander/talker/assets/ref_audio.wav \
  /opt/gnsis-asr/faster-whisper-small.en/model.bin; do
  if [ ! -e "$path" ]; then
    echo "start-voice: /models contains:" >&2
    find /models -maxdepth 3 2>/dev/null | head -40 >&2
    fail "missing $path. Is the volume with the weights mounted at /models?"
  fi
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

# Stop the pod after GNSIS_IDLE_STOP_MINUTES (default 10) with no live
# session, as Modal scaled to zero; it outlives the exec below.
python /usr/local/bin/idle_stop.py &

cd /workspace
exec gnsis-serve --config "$CONFIG"
