#!/usr/bin/env bash
# Start the GNSIS voice runtime on a RunPod pod.
#
# Refuses to start, with a message that names the fix, when the pod is not set
# up the way the runtime needs: the edge secret, the weights on the network
# volume at /models, two GPUs, and complete memory credentials.
set -euo pipefail

CONFIG="${GNSIS_CONFIG:-/workspace/runtime/configs/gnsis-voice.yaml}"

# Everything this pod prints also goes to a file the runtime serves at /logs,
# behind the edge secret, since RunPod's API does not expose a pod's logs. The
# file is on the pod volume (/models), which survives a stop, and the last
# LOG_BOOTS boots are kept beside it as runtime.log.1 (newest) and up.
LOG_FILE="${GNSIS_LOG_FILE:-/models/gnsis-logs/runtime.log}"
LOG_BOOTS="${GNSIS_LOG_BOOTS:-30}"
# /logs?boot=N serves up to 30 earlier boots; keep no more than it can serve.
case "$LOG_BOOTS" in ''|*[!0-9]*) LOG_BOOTS=30 ;; esac
[ "$LOG_BOOTS" -ge 1 ] || LOG_BOOTS=1
[ "$LOG_BOOTS" -le 30 ] || LOG_BOOTS=30
export GNSIS_LOG_FILE="$LOG_FILE"
mkdir -p "$(dirname "$LOG_FILE")"
if [ -f "$LOG_FILE" ]; then
  rm -f "$LOG_FILE.$LOG_BOOTS"
  for i in $(seq $((LOG_BOOTS - 1)) -1 1); do
    if [ -f "$LOG_FILE.$i" ]; then mv -f "$LOG_FILE.$i" "$LOG_FILE.$((i + 1))"; fi
  done
  mv -f "$LOG_FILE" "$LOG_FILE.1"
fi
# fd 3 keeps the real stderr, so a refusal reaches both places before exit.
exec 3>&2
exec > >(tee -a "$LOG_FILE") 2> >(tee -a "$LOG_FILE" >&3)

fail() {
  echo "start-voice: $*" | tee -a "$LOG_FILE" >&3
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

# The talker's Step-Audio2 voice synthesizer (minicpmo-utils 1.0.6 Token2wav)
# loads these files from this folder. If one is missing, re-copy just that
# folder from Modal, then verify, rather than boot into a talker that cannot
# synthesize speech. (minicpmo-utils 1.0.7 also wants config.yaml and
# hift_original.pt, which the volume does not have; it is pinned to 1.0.6.)
T2W=/models/Gander/talker/assets/token2wav
T2W_REQUIRED="speech_tokenizer_v2_25hz.onnx campplus.onnx flow.yaml flow.pt hift.pt"
t2w_incomplete=0
for f in $T2W_REQUIRED; do
  [ -e "$T2W/$f" ] || t2w_incomplete=1
done
if [ "$t2w_incomplete" = 1 ] && [ -n "${MODAL_TOKEN_ID:-}" ] && [ -n "${MODAL_TOKEN_SECRET:-}" ]; then
  volume="${GNSIS_MODELS_VOLUME:-gnsis-model-weights}"
  echo "start-voice: token2wav is incomplete; re-copying just that folder from Modal..." >&2
  python -m pip install --quiet "modal==1.5.0" >/dev/null 2>&1 || true
  stage=/models/.t2w-staging
  rm -rf "$stage" && mkdir -p "$stage"
  if modal volume get --env "${MODAL_ENVIRONMENT:-main}" "$volume" Gander/talker/assets/token2wav "$stage"; then
    src="$(find "$stage" -type f -name flow.yaml -printf '%h\n' 2>/dev/null | head -1)"
    [ -n "$src" ] || src="$(find "$stage" -type d -name token2wav | head -1)"
    [ -n "$src" ] || src="$stage"
    mkdir -p "$T2W"
    cp -rn "$src/." "$T2W/" 2>/dev/null || true
  fi
  rm -rf "$stage"
fi
for f in $T2W_REQUIRED; do
  if [ ! -e "$T2W/$f" ]; then
    echo "start-voice: token2wav folder contains:" >&2
    ls -la "$T2W" >&2 2>/dev/null || echo "  (folder missing)" >&2
    fail "token2wav is missing $f even after a dedicated copy. It is not on the Modal volume under Gander/talker/assets/token2wav; that file has to be added to the volume (or the image)."
  fi
done

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
