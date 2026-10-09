#!/usr/bin/env python3
"""Idempotently provision Coolify-hosted SimpleMem and enable GNSIS realtime memory.

Secrets are generated/handled in-process and never printed. The workflow uses
Coolify's own API; it does not use Railway or change public GNSIS API DNS.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

TARGET_REPO = "aubincorinaldiecooper-bit/gnsisbackend"
SIMPLEMEM_URL_DEFAULT = "https://simplemem.gnsis.studio"
API_URL = "https://api.gnsis.studio"
POLL_INTERVAL = 10
HEALTH_TIMEOUT = 600


class SetupError(RuntimeError):
    pass


def repo_name(value: Any) -> str:
    name = str(value or "").lower().strip().removesuffix(".git")
    for prefix in ("https://github.com/", "http://github.com/", "git@github.com:"):
        if name.startswith(prefix):
            name = name[len(prefix):]
    return name.strip("/")


def records(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict) and isinstance(value.get("data"), list):
        return records(value["data"])
    raise SetupError("Coolify returned an unexpected resource-list shape; no further changes made.")


class Coolify:
    def __init__(self) -> None:
        base = os.getenv("COOLIFY_URL", "https://coolify.gnsis.studio").rstrip("/")
        if base != "https://coolify.gnsis.studio":
            raise SetupError("COOLIFY_URL must be https://coolify.gnsis.studio")
        self.base = base + "/api/v1"
        self.token = os.getenv("COOLIFY_API_TOKEN", "")
        if not self.token:
            raise SetupError("GitHub Actions secret COOLIFY_API_TOKEN is missing.")

    def request(self, method: str, route: str, body: dict[str, Any] | None = None) -> Any:
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.base + route,
            data=data,
            method=method,
            headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "gnsis-simplemem-rollout/1",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                payload = response.read(4_000_000)
                return json.loads(payload) if payload else {}
        except urllib.error.HTTPError as exc:
            # Never include response bodies: Coolify errors can contain values.
            raise SetupError(f"Coolify {method} {route.split('?')[0]} returned HTTP {exc.code}.") from None
        except urllib.error.URLError:
            raise SetupError("Coolify API is unreachable; check URL, TLS, and API access.") from None


def app_detail(client: Coolify, app: dict[str, Any]) -> dict[str, Any]:
    detail = client.request("GET", "/applications/" + urllib.parse.quote(str(app["uuid"]), safe=""))
    if not isinstance(detail, dict):
        raise SetupError("Coolify application detail response was not an object.")
    return detail


def app_envs(client: Coolify, uuid: str) -> list[dict[str, Any]]:
    return records(client.request("GET", "/applications/" + urllib.parse.quote(uuid, safe="") + "/envs"))


def env_value(envs: list[dict[str, Any]], key: str) -> str:
    for item in envs:
        if item.get("key") == key:
            value = item.get("real_value")
            if not isinstance(value, str) or not value:
                value = item.get("value")
            return value if isinstance(value, str) else ""
    return ""


def set_env(client: Coolify, uuid: str, key: str, value: str, *, secret: bool = False) -> None:
    route = "/applications/" + urllib.parse.quote(uuid, safe="") + "/envs"
    existing = {item.get("key") for item in app_envs(client, uuid)}
    body = {
        "key": key,
        "value": value,
        "is_preview": False,
        "is_literal": True,
        "is_shown_once": secret,
    }
    client.request("PATCH" if key in existing else "POST", route, body)


def choose_unique(items: list[dict[str, Any]], label: str) -> dict[str, Any]:
    if len(items) != 1:
        raise SetupError(f"Expected exactly one {label} Coolify application; found {len(items)}. Refusing to guess.")
    return items[0]


def get_identity(detail: dict[str, Any], key: str, nested: str | None = None) -> str:
    value = detail.get(key)
    if value:
        return str(value)
    if nested:
        for container_key in ("project", "server", "environment", "destination"):
            container = detail.get(container_key)
            if isinstance(container, dict) and container.get(nested):
                return str(container[nested])
    return ""


def request_json(url: str, *, method: str = "GET", token: str | None = None, body: dict[str, Any] | None = None, timeout: float = 10) -> tuple[int, Any]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            payload = response.read(1_000_000)
            return response.status, json.loads(payload) if payload else {}
    except urllib.error.HTTPError as exc:
        return exc.code, {}
    except urllib.error.URLError:
        return 0, {}


def wait_for_simplemem(url: str) -> dict[str, Any]:
    deadline = time.monotonic() + HEALTH_TIMEOUT
    last_status = 0
    while time.monotonic() < deadline:
        status, payload = request_json(url.rstrip("/") + "/health", timeout=10)
        last_status = status
        if status == 200 and isinstance(payload, dict) and payload.get("ok") is True and payload.get("authConfigured") is True:
            return payload
        time.sleep(POLL_INTERVAL)
    raise SetupError(
        f"SimpleMem health did not become ready at {url}/health within {HEALTH_TIMEOUT}s "
        f"(last HTTP status {last_status}). The worker was not changed."
    )


def wait_for_app(client: Coolify, uuid: str, label: str, timeout: int = 600) -> None:
    deadline = time.monotonic() + timeout
    last_status = "unknown"
    while time.monotonic() < deadline:
        detail = client.request("GET", "/applications/" + urllib.parse.quote(uuid, safe=""))
        if isinstance(detail, dict):
            last_status = str(detail.get("status") or "unknown")
            if last_status.lower().startswith("running"):
                return
            if "exited" in last_status.lower() or "failed" in last_status.lower():
                raise SetupError(f"{label} deployment entered status {last_status}.")
        time.sleep(POLL_INTERVAL)
    raise SetupError(f"{label} did not reach a running status within {timeout}s (last status {last_status}).")


def deploy_app(client: Coolify, uuid: str, label: str) -> None:
    route = "/deploy?" + urllib.parse.urlencode({"uuid": uuid})
    response = client.request("POST", route)
    if not isinstance(response, dict) or not (response.get("deployments") or response.get("deployment_uuid")):
        raise SetupError(f"Coolify did not confirm a queued deployment for {label}.")
    print(f"- Queued {label} deployment.")


def main() -> int:
    client = Coolify()
    applications = records(client.request("GET", "/applications"))
    relevant = [
        app for app in applications
        if "gnsis" in str(app.get("name") or "").lower()
        or "simplemem" in str(app.get("name") or "").lower()
        or repo_name(app.get("git_repository") or app.get("git_full_url")) == TARGET_REPO
    ]
    simplemem_matches = [app for app in relevant if "simplemem" in str(app.get("name") or "").lower()]
    worker_matches = [
        app for app in relevant
        if "worker" in str(app.get("name") or "").lower()
        and repo_name(app.get("git_repository") or app.get("git_full_url")) == TARGET_REPO
    ]
    if not worker_matches:
        worker_matches = [
            app for app in relevant
            if "gnsisworker" in "".join(c for c in str(app.get("name") or "").lower() if c.isalnum())
        ]
    worker = choose_unique(worker_matches, "GNSIS worker")
    worker_detail = app_detail(client, worker)
    worker_envs = app_envs(client, str(worker["uuid"]))

    api_matches = [
        app for app in relevant
        if "api" in str(app.get("name") or "").lower()
        and "worker" not in str(app.get("name") or "").lower()
        and "beat" not in str(app.get("name") or "").lower()
        and repo_name(app.get("git_repository") or app.get("git_full_url")) == TARGET_REPO
    ]
    api_app = choose_unique(api_matches, "GNSIS API")
    api_envs = app_envs(client, str(api_app["uuid"]))
    api_key = env_value(api_envs, "GNSIS_API_KEY")
    if not api_key:
        raise SetupError(
            "The GNSIS API application does not expose GNSIS_API_KEY to the Coolify automation token. "
            "No service or worker settings were changed; configure the internal deploy key's API access first."
        )

    worker_repo = repo_name(worker_detail.get("git_repository") or worker_detail.get("git_full_url"))
    if worker_repo != TARGET_REPO:
        raise SetupError("The selected worker is not sourced from the expected GNSISBACKEND repository.")
    project_uuid = get_identity(worker_detail, "project_uuid", "uuid")
    server_uuid = get_identity(worker_detail, "server_uuid", "uuid")
    environment_uuid = str(worker_detail.get("environment_id") or worker_detail.get("environment_uuid") or "")
    environment_name = str(worker_detail.get("environment_name") or "")
    if not environment_name and isinstance(worker_detail.get("environment"), dict):
        environment_name = str(worker_detail["environment"].get("name") or "")
    github_app_uuid = str(worker_detail.get("github_app_uuid") or "")
    git_repository = str(worker_detail.get("git_repository") or worker_detail.get("git_full_url") or "")
    git_branch = str(worker_detail.get("git_branch") or "main")
    destination_uuid = str(worker_detail.get("destination_id") or worker_detail.get("destination_uuid") or "")
    if not all((project_uuid, server_uuid, environment_uuid or environment_name, github_app_uuid, git_repository)):
        raise SetupError(
            "The GNSIS worker does not expose the project/server/environment/GitHub-App identifiers "
            "needed to create a private-repository app safely. No service or worker settings were changed."
        )

    # Prefer an already configured HTTPS endpoint; otherwise use the dedicated
    # hostname. The health gate below prevents wiring the worker to a dead URL.
    existing_url = env_value(worker_envs, "GNSIS_SIMPLEMEM_URL").strip().rstrip("/")
    simplemem_url = existing_url or SIMPLEMEM_URL_DEFAULT
    parsed = urllib.parse.urlsplit(simplemem_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise SetupError("GNSIS_SIMPLEMEM_URL must be a base HTTPS URL with no path, query, or fragment.")

    if len(simplemem_matches) > 1:
        raise SetupError("Multiple Coolify apps contain SimpleMem in the name; refusing to guess.")
    token = secrets.token_urlsafe(48)
    if simplemem_matches:
        simplemem_app = simplemem_matches[0]
        simplemem_uuid = str(simplemem_app["uuid"])
        print(f"- Reusing existing SimpleMem application: {simplemem_app.get('name')} ({simplemem_uuid}).")
    else:
        create_body: dict[str, Any] = {
            "project_uuid": project_uuid,
            "server_uuid": server_uuid,
            "environment_name": environment_name or "production",
            "environment_uuid": environment_uuid,
            "github_app_uuid": github_app_uuid,
            "git_repository": git_repository,
            "git_branch": git_branch,
            "build_pack": "dockerfile",
            "ports_exposes": "8000",
            "name": "GNSIS SimpleMem",
            "description": "Private GNSIS realtime episodic memory sidecar",
            "domains": simplemem_url,
            "instant_deploy": False,
        }
        if destination_uuid:
            create_body["destination_uuid"] = destination_uuid
        created = client.request("POST", "/applications/private-github-app", create_body)
        if not isinstance(created, dict) or not created.get("uuid"):
            raise SetupError("Coolify did not return a UUID for the new SimpleMem application.")
        simplemem_uuid = str(created["uuid"])
        print(f"- Created Coolify SimpleMem application ({simplemem_uuid}).")

    # Configure the app before deploying it. Values are never printed.
    patch_body: dict[str, Any] = {
        "name": "GNSIS SimpleMem",
        "description": "Private GNSIS realtime episodic memory sidecar",
        "build_pack": "dockerfile",
        "dockerfile_location": "/Dockerfile.simplemem",
        "ports_exposes": "8000",
        "domains": simplemem_url,
        "instant_deploy": False,
    }
    client.request("PATCH", "/applications/" + urllib.parse.quote(simplemem_uuid, safe=""), patch_body)
    set_env(client, simplemem_uuid, "SIMPLEMEM_INTERNAL_TOKEN", token, secret=True)
    set_env(client, simplemem_uuid, "SIMPLEMEM_DATA_DIR", "/data/simplemem")
    storages_response = client.request("GET", "/applications/" + urllib.parse.quote(simplemem_uuid, safe="") + "/storages")
    if isinstance(storages_response, dict):
        storage_list = storages_response.get("persistent_storages", []) or storages_response.get("storages", [])
    else:
        storage_list = storages_response if isinstance(storages_response, list) else []
    if not any(isinstance(item, dict) and item.get("mount_path") == "/data/simplemem" for item in storage_list):
        client.request(
            "POST",
            "/applications/" + urllib.parse.quote(simplemem_uuid, safe="") + "/storages",
            {"type": "persistent", "name": "gnsis-simplemem-data", "mount_path": "/data/simplemem"},
        )
        print("- Added persistent storage at /data/simplemem.")
    deploy_app(client, simplemem_uuid, "SimpleMem")
    health = wait_for_simplemem(simplemem_url)
    print(
        "- SimpleMem HTTPS health verified: ok=true, authConfigured=true, "
        + "archiveRequired=" + str((health.get("archive") or {}).get("required", False))
    )

    worker_uuid = str(worker["uuid"])
    set_env(client, worker_uuid, "GNSIS_SIMPLEMEM_URL", simplemem_url)
    set_env(client, worker_uuid, "GNSIS_SIMPLEMEM_TOKEN", token, secret=True)
    set_env(client, worker_uuid, "GNSIS_SIMPLEMEM_INTERNAL_TOKEN", token, secret=True)
    print("- Configured the Coolify worker with the SimpleMem URL and both token aliases.")
    deploy_app(client, worker_uuid, "GNSIS worker")
    wait_for_app(client, worker_uuid, "GNSIS worker")
    print("- GNSIS worker is running with the new environment.")

    status, queued = request_json(
        API_URL + "/internal/compute/gnsis/deploy",
        method="POST",
        token=api_key,
        body={"confirm": "gnsis-voice", "smoke": True},
        timeout=30,
    )
    if status != 202 or not isinstance(queued, dict) or not queued.get("task_id"):
        raise SetupError(
            f"The GNSIS worker was updated, but Modal deployment could not be queued through the internal API (HTTP {status})."
        )
    task_id = str(queued["task_id"])
    print("- Queued the Modal GNSIS realtime deployment through the GNSIS API/worker path.")
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        status, task = request_json(
            API_URL + "/internal/compute/tasks/" + urllib.parse.quote(task_id, safe=""),
            token=api_key,
            timeout=20,
        )
        if status == 200 and isinstance(task, dict) and task.get("ready") is True:
            if task.get("successful") is not True:
                raise SetupError(
                    "Modal deployment task finished unsuccessfully; inspect the Coolify worker logs. "
                    "The task error details were intentionally not printed."
                )
            print("- Modal deployment task succeeded; worker reports the deployment health result.")
            print("- SUCCESS: Coolify SimpleMem is persistent, authenticated, and wired to the Modal realtime runtime.")
            return 0
        time.sleep(POLL_INTERVAL)
    raise SetupError(
        "Modal deployment task did not finish within 900 seconds. The Coolify worker and SimpleMem are configured; "
        "inspect the GNSIS worker task logs before retrying."
    )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SetupError as exc:
        print("SETUP BLOCKED: " + str(exc), file=sys.stderr)
        sys.exit(1)
