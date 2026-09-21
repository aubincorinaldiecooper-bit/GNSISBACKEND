#!/usr/bin/env python3
"""Fail unless the deployed Ornith brain is reachable, and say what it serves.

Run after ``modal deploy modal/ornith.py``. By default it looks the deployed
function up by name and asks Modal for its web address, which does not start a
container. The address is printed so the cutover in docs/live_runtime.md can
put it in the runtime's configuration.

With ``ORNITH_SMOKE=1`` it also asks the server which models it serves, which
cold-starts a GPU: several minutes, and it costs money. That needs
``ORNITH_API_KEY``, the same key the Modal secret holds.

With ``ORNITH_BASE_URL`` set it skips the Modal lookup entirely and asks that
address instead, with no Modal token, so the service being replaced can be read
the same way and the two compared.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

import modal

BASE_URL = os.environ.get("ORNITH_BASE_URL", "").strip()
APP_NAME = os.environ.get("ORNITH_MODAL_APP_NAME", "gnsis-ornith")
# Fixed, not configurable: a Modal function is named by its `def`, and
# modal/ornith.py always publishes this one.
FUNCTION_NAME = "ornith_server_v2"
ENVIRONMENT = os.environ.get("MODAL_ENVIRONMENT", "main")
SMOKE = os.environ.get("ORNITH_SMOKE", "").strip().lower() not in {"", "0", "false", "no"}
# A cold start pulls the weights onto the GPU before vLLM answers.
SMOKE_TIMEOUT_SEC = float(os.environ.get("ORNITH_SMOKE_TIMEOUT_SEC", "1800"))
# What the runtime's worker configuration asks for by name.
EXPECTED_MODEL = os.environ.get("ORNITH_SERVED_MODEL_NAME", "ornith")


def main() -> int:
    if BASE_URL:
        return smoke(BASE_URL.rstrip("/"))

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
    print(
        "Put this address in live/configs/gnsis-live.yaml under worker.settings.base_url "
        "and redeploy the live runtime to switch it over."
    )

    if not SMOKE:
        print("Skipped asking what it serves (set ORNITH_SMOKE=1 to start a container and ask).")
        return 0
    return smoke(url)


def smoke(url: str) -> int:
    """Ask the server which models it serves, and check ours is one of them."""

    api_key = (os.environ.get("ORNITH_API_KEY") or "").strip()
    if not api_key:
        print(
            "ERROR: ORNITH_API_KEY is required to ask the server anything; "
            "it is the key the Modal secret holds",
            file=sys.stderr,
        )
        return 1

    request = urllib.request.Request(
        f"{url}/v1/models",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=SMOKE_TIMEOUT_SEC) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Never print the body of a failed authenticated request: it is the one
        # place a server might echo what was sent to it.
        print(f"ERROR: {url}/v1/models answered {exc.code} {exc.reason}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"ERROR: {url}/v1/models did not answer: {exc}", file=sys.stderr)
        return 1

    served = [
        str(entry.get("id"))
        for entry in (body.get("data") or [])
        if isinstance(entry, dict) and entry.get("id")
    ]
    if EXPECTED_MODEL not in served:
        print(
            f"ERROR: {url} serves {served or 'nothing'}, not {EXPECTED_MODEL!r}, "
            "which is the name the runtime's worker configuration asks for",
            file=sys.stderr,
        )
        return 1
    print(f"OK: {url} serves {', '.join(served)} and answers an authenticated request")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
