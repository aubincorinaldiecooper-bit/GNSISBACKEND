"""Stop this RunPod pod after it has served no live session for a while.

The Modal app scaled to zero after five idle minutes on its own; a RunPod pod
bills until something stops it. This watcher runs beside the runtime, polls its
local /health, and stops the pod once no session has been active for
GNSIS_IDLE_STOP_MINUTES (default 10; 0 turns it off).

"Stop", not "terminate": the pod volume, and the weights copied to /models on
first boot, survive, so a restart does not copy them again.

Idle means the runtime answered and reported no active session and no model
work in progress. While the model is still loading it is never idle, so a slow
first boot is not cut short. A runtime that failed to start counts as idle, so
a broken pod stops instead of billing indefinitely.

RunPod sets RUNPOD_POD_ID and a pod-scoped RUNPOD_API_KEY inside every pod;
those are what stop it. Standard library only.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any

HEALTH_URL = "http://127.0.0.1:7975/health"
POLL_SEC = 30


def log(message: str) -> None:
    print(f"idle-stop: {message}", file=sys.stderr, flush=True)


def read_health() -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=10) as resp:
            return json.loads(resp.read(200_000) or b"{}")
    except Exception:  # noqa: BLE001 - not answering yet is a state, not an error
        return None


def is_idle(health: dict[str, Any] | None) -> bool:
    """Whether this /health reading counts toward the idle timer."""
    if health is None:
        # Not answering: still starting up. Never stop a pod mid-boot.
        return False
    state = str(health.get("runtime_state") or health.get("state") or "").lower()
    if state in {"loading", "starting"}:
        return False
    if state in {"failed", "error"}:
        return True
    if "active_sessions" not in health:
        # The bootstrap app answers before the model is ready.
        return False
    return not health.get("active_sessions") and not health.get("busy")


def stop_pod() -> bool:
    pod_id = os.environ.get("RUNPOD_POD_ID", "").strip()
    key = os.environ.get("RUNPOD_API_KEY", "").strip()
    if not (pod_id and key):
        log("RUNPOD_POD_ID or RUNPOD_API_KEY is not set in this pod; cannot stop it.")
        return False
    attempts = (
        (
            f"https://rest.runpod.io/v1/pods/{pod_id}/stop",
            b"",
            {"Authorization": "Bearer " + key},
        ),
        (
            "https://api.runpod.io/graphql",
            json.dumps(
                {"query": f'mutation {{ podStop(input: {{podId: "{pod_id}"}}) {{ id desiredStatus }} }}'}
            ).encode(),
            {"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        ),
    )
    for url, body, headers in attempts:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = resp.read(2000).decode("utf-8", "replace")
                if resp.status < 300 and '"errors"' not in payload:
                    log(f"stop requested for pod {pod_id} via {url}")
                    return True
                log(f"{url} answered {resp.status}: {payload[:300]}")
        except urllib.error.HTTPError as exc:
            log(f"{url} -> HTTP {exc.code}: {exc.read(300).decode('utf-8', 'replace')}")
        except Exception as exc:  # noqa: BLE001
            log(f"{url} -> {exc.__class__.__name__}: {exc}")
    return False


def main() -> int:
    try:
        minutes = float(os.environ.get("GNSIS_IDLE_STOP_MINUTES", "10"))
    except ValueError:
        log("GNSIS_IDLE_STOP_MINUTES is not a number; idle stop is off.")
        return 1
    if minutes <= 0:
        log("idle stop is off (GNSIS_IDLE_STOP_MINUTES <= 0).")
        return 0
    limit = minutes * 60
    log(f"stopping this pod after {minutes:g} idle minutes.")
    idle_since: float | None = None
    while True:
        now = time.monotonic()
        if is_idle(read_health()):
            idle_since = idle_since if idle_since is not None else now
            if now - idle_since >= limit:
                if stop_pod():
                    return 0
                # Could not stop it: say so every interval, keep the runtime up.
                log("could not stop the pod; it keeps billing. Stop it from the RunPod console.")
        else:
            idle_since = None
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    sys.exit(main())
