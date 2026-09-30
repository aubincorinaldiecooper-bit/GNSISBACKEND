#!/usr/bin/env bash
# Run one end-to-end action scenario: the real runtime app with a scripted
# model (runtime/gnsis_runtime/tests/scripted_runtime.py) and the desktop's
# real action path (desktop/scripts/e2e-actions.ts).
#
#   desktop/scripts/e2e-actions.sh files-move
#   desktop/scripts/e2e-actions.sh files-move-asked
#   desktop/scripts/e2e-actions.sh files-move-spoken
#   desktop/scripts/e2e-actions.sh files-move-typed
#   desktop/scripts/e2e-actions.sh open-app '{"name":"open","arguments":{"target":"TextEdit"}}'
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SCENARIO="${1:-files-move}"
MOVE='{"name":"files","arguments":{"action":"move","path":"report.pdf","to":"Projects"}}'
CALL="${2:-$MOVE}"
if [ -n "${E2E_PORT:-}" ]; then
  PORT="$E2E_PORT"
else
  # Each workflow step launches and tears down its own runtime. Reusing one
  # fixed port lets the next step briefly hit the previous process while Linux
  # is still reaping it, so health can pass and the WebSocket can then vanish.
  # Pick a fresh loopback port for every invocation.
  PORT="$(python3 - <<'PY'
import socket
with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    print(s.getsockname()[1])
PY
)"
fi
WORK="$(mktemp -d)"
# Two plain branches rather than an optional-arguments array: macOS ships
# bash 3.2, where expanding an empty array under `set -u` is an error.
if [ "$SCENARIO" = "files-move-spoken" ]; then
  python3 "$ROOT/runtime/gnsis_runtime/tests/scripted_runtime.py" \
    --port "$PORT" --media-dir "$WORK/media" --call "$CALL" \
    --asr-text "Move that report into the Projects folder." >"$WORK/runtime.log" 2>&1 &
else
  python3 "$ROOT/runtime/gnsis_runtime/tests/scripted_runtime.py" \
    --port "$PORT" --media-dir "$WORK/media" --call "$CALL" >"$WORK/runtime.log" 2>&1 &
fi
RUNTIME_PID=$!
cleanup() {
  kill "$RUNTIME_PID" 2>/dev/null || true
  wait "$RUNTIME_PID" 2>/dev/null || true
}
trap cleanup EXIT

ready=0
for _ in $(seq 1 150); do
  if ! kill -0 "$RUNTIME_PID" 2>/dev/null; then
    echo "runtime exited before becoming healthy"
    cat "$WORK/runtime.log"
    exit 1
  fi
  if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 0.2
done
if [ "$ready" -ne 1 ]; then
  echo "runtime never became healthy on port $PORT"
  cat "$WORK/runtime.log"
  exit 1
fi
cd "$ROOT/desktop"
status=0
E2E_RUNTIME_URL="http://127.0.0.1:$PORT" E2E_MEDIA_DIR="$WORK/media" \
  E2E_SCENARIO="$SCENARIO" E2E_HOME="$WORK/home" npx tsx scripts/e2e-actions.ts || status=$?
echo "--- runtime log: what it agreed and logged ---"
grep -E "host tools|Uvicorn running" "$WORK/runtime.log" | head -5 || true
if [ "$status" -ne 0 ]; then grep -A 30 -E "Traceback|ERROR" "$WORK/runtime.log" | head -60; fi
exit "$status"
