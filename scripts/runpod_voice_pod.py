#!/usr/bin/env python3
"""Create and operate the gnsis-voice pod on RunPod.

Usage (normally from .github/workflows/runpod-voice-pod.yml):

    python scripts/runpod_voice_pod.py create|status|stop|start|terminate

The pod mirrors the former Modal app (modal/gnsis_voice.py): 2x L40S, the
gnsis-voice image from GitHub's container registry, port 7975 over HTTPS, a
persistent pod volume at /models for the weights, and the same secrets. Those
secrets are copied from the GNSIS worker's environment on Coolify, where they
already live, so nothing is entered twice. No secret value is ever printed.

Environment:
  RUNPOD_API_KEY      required.
  COOLIFY_API_TOKEN   required for create (reads the worker's environment).
  GHCR_PULL_TOKEN     optional; a GitHub token with read:packages. When set,
                      it is registered with RunPod so the pod can pull the
                      private image. Without it the image must be public.
  IMAGE_TAG           optional; defaults to "latest".
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import coolify_simplemem_setup as coolify  # noqa: E402

API = "https://rest.runpod.io/v1"
USER_AGENT = "Mozilla/5.0 (compatible; gnsis-runpod-pod/1.0; +https://github.com/aubincorinaldiecooper-bit/GNSISBACKEND)"
POD_NAME = "gnsis-voice"
PORT = 7975
REGISTRY_AUTH_NAME = "ghcr-gnsis-voice"
# A priority list of interchangeable 48 GB cards, not L40S alone: the runtime
# only needs two 48 GB GPUs (Thinker on GPU 0, detached Talker on GPU 1), and
# RunPod treats gpuTypeIds as "any of these, in order". Accepting L40/A40/A6000
# as well as L40S means a shortage of one card no longer blocks the pod.
GPU_TYPES = ["NVIDIA L40S", "NVIDIA L40", "NVIDIA A40", "NVIDIA RTX A6000"]
GPU_COUNT = 2
VOLUME_GB = 150  # MiniCPM-o-4_5 + Thinker + Talker, with headroom
CONTAINER_DISK_GB = 60
# Copied from the worker when present. GNSIS_EDGE_SECRET is required.
WORKER_ENV_KEYS = (
    "GNSIS_EDGE_SECRET",
    "GNSIS_SIMPLEMEM_URL",
    "GNSIS_SIMPLEMEM_TOKEN",
    "GNSIS_SIMPLEMEM_INTERNAL_TOKEN",
    "GNSIS_DESKTOP_GRANT_PUBLIC_KEY",
    "GNSIS_DESKTOP_GRANT_ISSUER",
    # Only for the one-time weight copy on first boot.
    "MODAL_TOKEN_ID",
    "MODAL_TOKEN_SECRET",
    "MODAL_ENVIRONMENT",
    "GNSIS_MODELS_VOLUME",
)


class PodError(RuntimeError):
    pass


def _unquote(value: str) -> str:
    """Trim whitespace and one pair of surrounding quotes.

    Coolify sometimes stores a value with the quotes included (e.g.
    GNSIS_MODELS_VOLUME as 'gnsis-model-weights'), which then reaches the pod
    verbatim and breaks commands like `modal volume get` that reject the quoted
    name. The quotes are never part of the real value.
    """
    value = (value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1].strip()
    return value


def runpod(method: str, route: str, body: dict[str, Any] | None = None) -> Any:
    key = os.environ.get("RUNPOD_API_KEY", "").strip()
    if not key:
        raise PodError("RUNPOD_API_KEY is not set.")
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        API + route,
        data=data,
        method=method,
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            # rest.runpod.io sits behind Cloudflare, which bans Python's default
            # Python-urllib User-Agent (Error 1010, browser_signature_banned).
            # Identify a normal client, as the SimpleMem rollout does.
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        # RunPod's error bodies describe the problem (e.g. no capacity) and do
        # not echo request secrets; keep them visible but bounded.
        detail = exc.read(1500).decode("utf-8", "replace")
        raise PodError(f"RunPod {method} {route} -> HTTP {exc.code}: {detail}") from None


def find_pod() -> dict[str, Any] | None:
    pods = runpod("GET", "/pods")
    matches = [p for p in (pods if isinstance(pods, list) else []) if p.get("name") == POD_NAME]
    if len(matches) > 1:
        raise PodError(f"{len(matches)} pods are named {POD_NAME}; refusing to guess.")
    return matches[0] if matches else None


def pod_url(pod_id: str) -> str:
    return f"https://{pod_id}-{PORT}.proxy.runpod.net"


def worker_environment() -> dict[str, str]:
    client = coolify.Coolify()
    apps = coolify.records(client.request("GET", "/applications"))
    workers = [
        a
        for a in apps
        if "worker" in str(a.get("name") or "").lower()
        and coolify.repo_name(a.get("git_repository") or a.get("git_full_url")) == coolify.TARGET_REPO
    ]
    worker = coolify.choose_unique(workers, "GNSIS worker")
    envs = coolify.app_envs(client, str(worker["uuid"]))
    values = {k: _unquote(coolify.env_value(envs, k)) for k in WORKER_ENV_KEYS}
    values = {k: v for k, v in values.items() if v}
    if not values.get("GNSIS_EDGE_SECRET"):
        raise PodError(
            "The GNSIS worker on Coolify has no readable GNSIS_EDGE_SECRET. The pod refuses to "
            "start without it, so no pod was created."
        )
    return values


def registry_auth_id() -> str | None:
    token = os.environ.get("GHCR_PULL_TOKEN", "").strip()
    if not token:
        print("- GHCR_PULL_TOKEN not set: assuming the image is public.")
        return None
    existing = runpod("GET", "/containerregistryauth")
    for item in existing if isinstance(existing, list) else []:
        if item.get("name") == REGISTRY_AUTH_NAME:
            print(f"- Reusing RunPod registry login {REGISTRY_AUTH_NAME}.")
            return str(item["id"])
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "").strip()
    if not owner:
        raise PodError("GITHUB_REPOSITORY_OWNER is not set; it is the registry username.")
    created = runpod(
        "POST",
        "/containerregistryauth",
        {"name": REGISTRY_AUTH_NAME, "username": owner, "password": token},
    )
    print(f"- Registered RunPod registry login {REGISTRY_AUTH_NAME}.")
    return str(created["id"])


def create() -> None:
    existing = find_pod()
    if existing:
        print(f"- Pod {POD_NAME} already exists ({existing['id']}); not creating another.")
        status()
        return
    owner = (os.environ.get("GITHUB_REPOSITORY_OWNER") or "").lower()
    tag = os.environ.get("IMAGE_TAG", "").strip() or "latest"
    image = f"ghcr.io/{owner}/gnsis-voice:{tag}"
    env = worker_environment()
    print("- Copied from the Coolify worker: " + ", ".join(sorted(env)))
    # The pod stops itself after this many minutes without a live session.
    env["GNSIS_IDLE_STOP_MINUTES"] = os.environ.get("GNSIS_IDLE_STOP_MINUTES", "").strip() or "10"
    if not (env.get("MODAL_TOKEN_ID") and env.get("MODAL_TOKEN_SECRET")):
        print("- WARNING: no Modal token on the worker; the first boot cannot copy the weights.")
    # Coolify does not expose secret values over its API, so the SimpleMem token
    # usually does not copy even when its URL does. The runtime requires the
    # token whenever the URL is set, so without it drop the URL and run this
    # pod without durable memory rather than fail to boot. Full-duplex voice
    # does not depend on memory; set GNSIS_SIMPLEMEM_TOKEN on the pod later to
    # turn it on.
    if env.get("GNSIS_SIMPLEMEM_URL") and not (
        env.get("GNSIS_SIMPLEMEM_TOKEN") or env.get("GNSIS_SIMPLEMEM_INTERNAL_TOKEN")
    ):
        env.pop("GNSIS_SIMPLEMEM_URL", None)
        print("- NOTE: SimpleMem token not readable from Coolify; this pod runs without durable memory.")

    # Secure Cloud first, then Community, so a lack of 2x L40S on one does not
    # block the test. Override with GNSIS_POD_CLOUD (comma-separated).
    cloud_order = [
        c.strip().upper()
        for c in (os.environ.get("GNSIS_POD_CLOUD", "SECURE,COMMUNITY")).split(",")
        if c.strip()
    ]
    base: dict[str, Any] = {
        "name": POD_NAME,
        "imageName": image,
        "computeType": "GPU",
        "gpuTypeIds": GPU_TYPES,
        "gpuCount": GPU_COUNT,
        "containerDiskInGb": CONTAINER_DISK_GB,
        "volumeInGb": VOLUME_GB,
        # Not /workspace: the runtime source lives there in the image.
        "volumeMountPath": "/models",
        "ports": [f"{PORT}/http"],
        "minRAMPerGPU": 24,
        "env": env,
    }
    auth = registry_auth_id()
    if auth:
        base["containerRegistryAuthId"] = auth

    gpus = " / ".join(GPU_TYPES)
    pod = None
    last_capacity_error = ""
    for cloud in cloud_order:
        try:
            pod = runpod("POST", "/pods", {**base, "cloudType": cloud})
            print(f"- Got capacity on {cloud} Cloud.")
            break
        except PodError as exc:
            if "no instances currently available" in str(exc).lower():
                last_capacity_error = str(exc)
                print(f"- No {GPU_COUNT}x [{gpus}] available on {cloud} Cloud; trying the next option.")
                continue
            raise
    if pod is None:
        raise PodError(
            f"No {GPU_COUNT}x [{gpus}] capacity on {' or '.join(cloud_order)} Cloud right now. "
            "RunPod capacity fluctuates: re-run create in a few minutes, or set GNSIS_POD_CLOUD / "
            f"widen the GPU choice. Last response: {last_capacity_error}"
        )
    pod_id = pod.get("id")
    print(f"- Created pod {POD_NAME} ({pod_id}) from {image} on {GPU_COUNT}x one of [{gpus}].")
    print(f"- Runtime address once booted: {pod_url(pod_id)}")
    print("- First boot copies the weights before the model loads; that takes a while.")


def status() -> None:
    pod = find_pod()
    if not pod:
        print(f"- No pod named {POD_NAME}.")
        return
    shown = {k: pod.get(k) for k in ("id", "desiredStatus", "lastStatusChange", "imageName", "costPerHr", "gpuCount")}
    print(json.dumps(shown, indent=1))
    url = pod_url(pod["id"]) + "/health"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "gnsis-runpod-pod/1.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            print(f"- {url} -> HTTP {resp.status}: {resp.read(600).decode('utf-8', 'replace')}")
    except Exception as exc:  # noqa: BLE001 - report, never fail status on it
        print(f"- {url} -> not answering yet ({exc.__class__.__name__}: {exc})")


def lifecycle(action: str) -> None:
    pod = find_pod()
    if not pod:
        raise PodError(f"No pod named {POD_NAME}.")
    if action == "terminate":
        runpod("DELETE", f"/pods/{pod['id']}")
        print(f"- Terminated {POD_NAME} ({pod['id']}). Its pod volume and copied weights are gone.")
        return
    runpod("POST", f"/pods/{pod['id']}/{action}")
    print(f"- Requested {action} for {POD_NAME} ({pod['id']}).")


def main(argv: list[str]) -> int:
    action = argv[1] if len(argv) > 1 else "status"
    try:
        if action == "create":
            create()
        elif action == "status":
            status()
        elif action in {"stop", "start", "terminate"}:
            lifecycle(action)
        else:
            raise PodError(f"Unknown action {action!r}; use create, status, stop, start or terminate.")
    except (PodError, coolify.SetupError) as exc:
        print("POD ACTION FAILED: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
