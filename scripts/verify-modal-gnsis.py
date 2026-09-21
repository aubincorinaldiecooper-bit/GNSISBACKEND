#!/usr/bin/env python3
"""Fail unless the deployed live runtime is reachable as a web server.

Run after ``modal deploy modal/gnsis.py``. It looks the deployed function up
by name and asks Modal for its web address, which does not start a container.
With ``GNSIS_SMOKE=1`` it also opens ``/health``, which does: the model loads
on a GPU first, so that is a cold start of several minutes, and it costs money.
The address is printed so the cutover in docs/live_runtime.md can use it.
With ``GNSIS_HEALTH_URL`` set, only ``/health`` at that address is opened, with
no Modal lookup and no token, so the app being replaced can be read the same way.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

import modal

# Set to any runtime's address to run only the /health check against it, with
# no Modal lookup and no token: this is how the app being replaced is read.
HEALTH_URL = os.environ.get("GNSIS_HEALTH_URL", "").strip()
APP_NAME = os.environ.get("MODAL_APP_NAME", "gnsis-live")
FUNCTION_NAME = os.environ.get("MODAL_FUNCTION_NAME", "gnsis_server")
ENVIRONMENT = os.environ.get("MODAL_ENVIRONMENT", "main")
SMOKE = os.environ.get("GNSIS_SMOKE", "").strip().lower() not in {"", "0", "false", "no"}
# A cold start loads MiniCPM-o on the GPU before the server answers.
SMOKE_TIMEOUT_SEC = float(os.environ.get("GNSIS_SMOKE_TIMEOUT_SEC", "1800"))

# (key in runtime/configs/gnsis-live.yaml, field name in /health). The health
# route reports a few settings under other names, and two of its fields are
# derived from several keys or from none, so the mapping is spelled out.
HEALTH_SETTINGS = (
    ("duplex.sliding_window_mode", "sliding_window_mode"),
    ("duplex.context_max_units", "context_max_units"),
    ("duplex.context_previous_max_tokens", "context_previous_max_tokens"),
    ("duplex.expose_task_slate_to_model", "task_slate_visible_to_model"),
    ("duplex.generate_audio", "generate_audio"),
    (
        "duplex.allow_client_video, client_video_mode and client_video_sources; "
        "vision_available and recommended_frame_rate are derived, not set",
        "client_video",
    ),
    ("asr.mode", "asr_enabled"),
    ("not a file setting, fixed by the model core", "chunk_ms"),
)


def main() -> int:
    if HEALTH_URL:
        return smoke(HEALTH_URL.rstrip("/"))
    deployed = modal.Function.from_name(
        APP_NAME,
        FUNCTION_NAME,
        environment_name=ENVIRONMENT,
    )
    try:
        deployed.hydrate()
    except Exception as exc:  # a missing app and a bad token both land here
        print(
            f"ERROR: could not find {APP_NAME}/{FUNCTION_NAME} in {ENVIRONMENT}: {exc}",
            file=sys.stderr,
        )
        return 1

    url = deployed.get_web_url()
    if not url:
        print(
            f"ERROR: {APP_NAME}/{FUNCTION_NAME} in {ENVIRONMENT} is deployed "
            "but has no web address, so there is nothing to connect to",
            file=sys.stderr,
        )
        return 1
    url = url.rstrip("/")
    print(f"OK: {APP_NAME}/{FUNCTION_NAME} in {ENVIRONMENT} is served at {url}")

    if not SMOKE:
        print("Skipped opening /health (set GNSIS_SMOKE=1 to start a container and open it).")
        return 0
    return smoke(url)


def smoke(url: str) -> int:
    """Open /health on the deployed runtime and check what it says about itself."""

    try:
        with urllib.request.urlopen(f"{url}/health", timeout=SMOKE_TIMEOUT_SEC) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, ValueError, OSError) as exc:
        print(f"ERROR: {url}/health did not answer: {exc}", file=sys.stderr)
        return 1

    status = body.get("status") if isinstance(body, dict) else None
    tools = body.get("tools") if isinstance(body, dict) else None
    if status != "ok":
        print(f"ERROR: {url}/health reports status {status!r}", file=sys.stderr)
        return 1
    if not isinstance(tools, list) or "haptic" not in tools:
        print(
            f"ERROR: {url}/health does not list the haptic output tool: {tools!r}",
            file=sys.stderr,
        )
        return 1
    print(f"OK: {url}/health answers with status ok and tools {', '.join(map(str, tools))}")
    # The settings the runtime reports about itself, named by the keys that
    # set them in runtime/configs/gnsis-live.yaml, because that is where a
    # difference has to be carried: the loader refuses any key it does not
    # know, and /health does not always use the file's name for a setting.
    # The cutover compares these between the app being replaced and the new
    # one; a difference means the checked-in configuration is not what ran.
    print("Settings reported by /health, named by their keys in runtime/configs/gnsis-live.yaml:")
    for key, health_field in HEALTH_SETTINGS:
        label = key if key.endswith(health_field) else f"{key} (health calls it {health_field})"
        print(f"  {label}: {body.get(health_field)!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
