#!/usr/bin/env python3
"""Inventory GNSIS resources in Coolify; optionally queue a strictly gated API deployment.

Reads resource metadata and variable names only. Never log API response bodies,
variable values, passwords, database URLs, or bearer tokens.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TARGET_REPO = "aubincorinaldiecooper-bit/gnsisbackend"
API_ENV_KEYS = {
    "DATABASE_URL",
    "REDIS_URL",
    "GNSIS_SERVICE_ROLE",
    "GITHUB_APP_ID",
    "GITHUB_APP_PRIVATE_KEY",
    "GITHUB_APP_SLUG",
    "GITHUB_WEBHOOK_SECRET",
    "OPENROUTER_API_KEY",
    "BETTER_AUTH_JWKS_URL",
    "BETTER_AUTH_ISSUER",
    "BETTER_AUTH_AUDIENCE",
    "GNSIS_AUTH_INTERNAL_URL",
    "GNSIS_AUTH_INTERNAL_SECRET",
    "GNSIS_FRONTEND_URL",
    "GNSIS_EXECUTION_PROVIDER",
    "GNSIS_PUBLIC_API_URL",
    "GNSIS_EXECUTOR_OWNER",
    "GNSIS_EXECUTOR_REPO",
    "GNSIS_EXECUTOR_WORKFLOW",
    "GNSIS_EXECUTOR_REF",
    "GNSIS_EXECUTOR_OIDC_ISSUER",
    "GNSIS_EXECUTOR_OIDC_AUDIENCE",
    "GNSIS_EXECUTOR_TRUSTED_WORKFLOW_SHA",
    "GNSIS_API_KEY",
    "GNSIS_VIRTUAL_KEY_PEPPER",
}


def repo_name(value):
    name = str(value or "").lower().strip().removesuffix(".git")
    for prefix in ("https://github.com/", "http://github.com/", "git@github.com:"):
        if name.startswith(prefix):
            name = name[len(prefix):]
    return name.strip("/")


def select_backend(apps, requested_uuid=""):
    candidates = []
    for app in apps:
        repository = repo_name(app.get("git_repository") or app.get("git_full_url"))
        name = "".join(char for char in str(app.get("name", "")).lower() if char.isalnum())
        if repository == TARGET_REPO or "gnsisbackend" in name:
            candidates.append(app)
    if requested_uuid:
        candidates = [app for app in candidates if app.get("uuid") == requested_uuid]
    if len(candidates) != 1:
        raise ValueError(
            "Could not uniquely identify GNSISBACKEND (matches: "
            + str(len(candidates))
            + "). Confirm the resource in Coolify; only then set COOLIFY_APPLICATION_UUID."
        )
    return candidates[0]


def choose_database(databases, label):
    candidates = [
        resource for resource in databases
        if label in str(resource.get("name", "")).lower()
        and "gnsis" in str(resource.get("name", "")).lower()
    ]
    if len(candidates) > 1:
        raise ValueError("Multiple GNSIS " + label + " resources; refusing to guess.")
    return candidates[0] if candidates else None


def records(obj):
    if isinstance(obj, list):
        return [item for item in obj if isinstance(item, dict)]
    if isinstance(obj, dict) and isinstance(obj.get("data"), list):
        return records(obj["data"])
    raise ValueError("Unexpected Coolify response shape; no changes made.")


class Coolify:
    def __init__(self, base_url, token):
        url = urllib.parse.urlsplit(str(base_url).rstrip("/"))
        if url.scheme != "https" or url.netloc != "coolify.gnsis.studio" or url.path not in ("", "/"):
            raise ValueError("COOLIFY_URL must be https://coolify.gnsis.studio")
        if not token:
            raise ValueError("GitHub Actions secret COOLIFY_API_TOKEN is missing.")
        self.endpoint = "https://coolify.gnsis.studio/api/v1"
        self.token = token

    def request(self, method, route):
        req = urllib.request.Request(
            self.endpoint + route,
            method=method,
            headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/json",
                "User-Agent": "gnsis-coolify-migration/1",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                body = response.read(4_000_000)
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            # Never include the API's error response; it may contain credentials.
            raise ValueError("Coolify request returned HTTP " + str(exc.code) + ".") from None
        except urllib.error.URLError:
            raise ValueError("Coolify endpoint unreachable; check TLS, address, and API access.") from None


def inventory(client, expected_uuid):
    applications = records(client.request("GET", "/applications"))
    app = select_backend(applications, expected_uuid)
    # Read-only inventory of GNSIS/worker/SimpleMem apps. Only resource
    # metadata and variable names are retained; secret values are never read
    # into the report.
    relevant_apps = []
    for candidate in applications:
        name = str(candidate.get("name") or "")
        lowered = name.lower()
        if "gnsis" not in lowered and "simplemem" not in lowered:
            continue
        env_items = records(
            client.request("GET", "/applications/" + str(candidate["uuid"]) + "/envs")
        )
        env_names = sorted(
            item["key"] for item in env_items
            if isinstance(item.get("key"), str)
        )
        relevant_apps.append({
            "name": name,
            "uuid": str(candidate.get("uuid") or ""),
            "status": str(candidate.get("status") or "unknown"),
            "environment_id": str(candidate.get("environment_id") or ""),
            "simplemem_url_configured": "GNSIS_SIMPLEMEM_URL" in env_names,
            "simplemem_token_configured": (
                "GNSIS_SIMPLEMEM_TOKEN" in env_names
                or "GNSIS_SIMPLEMEM_INTERNAL_TOKEN" in env_names
            ),
            "modal_credentials_configured": (
                "MODAL_TOKEN_ID" in env_names
                and "MODAL_TOKEN_SECRET" in env_names
            ),
            "env_names": env_names,
        })
    databases = records(client.request("GET", "/databases"))
    pg = choose_database(databases, "postgres")
    redis = choose_database(databases, "redis")
    envs = records(client.request("GET", "/applications/" + str(app["uuid"]) + "/envs"))
    names = {item.get("key") for item in envs if isinstance(item.get("key"), str)}
    storages = {}
    if pg:
        storages = client.request("GET", "/databases/" + str(pg["uuid"]) + "/storages")
    mounts = storages.get("persistent_storages", []) if isinstance(storages, dict) else []
    pg_volume_ok = any(
        isinstance(volume, dict) and volume.get("mount_path") == "/var/lib/postgresql"
        for volume in mounts
    )
    return {
        "app_uuid": str(app["uuid"]),
        "app_status": str(app.get("status") or "unknown"),
        "app_environment_id": str(app.get("environment_id") or ""),
        "app_destination_id": str(app.get("destination_id") or ""),
        "pg_found": pg is not None,
        "pg_running": bool(pg and str(pg.get("status") or "").startswith("running")),
        "pg_version_18": bool(pg and str(pg.get("image") or "").startswith("postgres:18")),
        "pg_volume_ok": pg_volume_ok,
        "pg_environment_id": str(pg.get("environment_id") or "") if pg else "",
        "pg_destination_id": str(pg.get("destination_id") or "") if pg else "",
        "redis_found": redis is not None,
        "redis_running": bool(redis and str(redis.get("status") or "").startswith("running")),
        "missing_keys": sorted(API_ENV_KEYS - names),
        "relevant_apps": sorted(relevant_apps, key=lambda item: item["name"].lower()),
        "simplemem_apps": [
            item for item in relevant_apps if "simplemem" in item["name"].lower()
        ],
        "worker_apps": [
            item for item in relevant_apps
            if "worker" in item["name"].lower() and "gnsis" in item["name"].lower()
        ],
    }


def blockers(result, approved_data):
    issues = []
    if not approved_data:
        issues.append("Production database restore and row-count verification not approved.")
    if not result["pg_running"] or not result["pg_version_18"] or not result["pg_volume_ok"]:
        issues.append("PostgreSQL 18, running state, or persistent storage not verified.")
    if not result["redis_running"]:
        issues.append("Redis for GNSIS missing or not running.")
    if not result["app_destination_id"] or result["app_destination_id"] != result["pg_destination_id"]:
        issues.append("Backend and PostgreSQL destination/network identity not verified.")
    if not result["app_environment_id"] or result["app_environment_id"] != result["pg_environment_id"]:
        issues.append("Backend and PostgreSQL are in different Coolify project environments.")
    if result["missing_keys"]:
        issues.append("Required API variable names missing (see report).")
    return issues


def summary_lines(result, issues):
    lines = [
        "## GNSIS Coolify migration audit",
        "",
        "- Backend application UUID: " + result["app_uuid"],
        "- Backend status: " + result["app_status"],
        "- PostgreSQL resource found: " + str(result["pg_found"]),
        "- PostgreSQL running: " + str(result["pg_running"]),
        "- PostgreSQL image matches 18: " + str(result["pg_version_18"]),
        "- PostgreSQL volume verified: " + str(result["pg_volume_ok"]),
        "- Redis found/running: " + str(result["redis_found"]) + "/" + str(result["redis_running"]),
        "- Backend and database same project environment: "
        + str(bool(result["app_environment_id"] and result["app_environment_id"] == result["pg_environment_id"])),
        "- Backend and database same destination: "
        + str(bool(result["app_destination_id"] and result["app_destination_id"] == result["pg_destination_id"])),
        "- Missing API env names: " + (", ".join(result["missing_keys"]) or "none"),
        "",
        "### Coolify realtime memory resources",
        "- SimpleMem application candidates: "
        + (
            ", ".join(
                item["name"] + " (" + item["uuid"] + "; " + item["status"] + ")"
                for item in result["simplemem_apps"]
            ) or "none found"
        ),
        "- GNSIS worker application candidates: "
        + (
            ", ".join(
                item["name"] + " (" + item["uuid"] + "; " + item["status"] + ")"
                for item in result["worker_apps"]
            ) or "none found"
        ),
    ]
    for item in result["relevant_apps"]:
        lines.append(
            "- " + item["name"] + ": SimpleMem URL key="
            + str(item["simplemem_url_configured"])
            + ", SimpleMem token key=" + str(item["simplemem_token_configured"])
            + ", Modal credentials=" + str(item["modal_credentials_configured"])
            + ", env names=" + (", ".join(item["env_names"]) or "none")
        )
    lines += [
        "",
        "### Deployment gates",
    ]
    lines += ["- BLOCKED: " + item for item in issues] if issues else ["- Ready for gated API-only deploy."]
    lines += [
        "",
        "Variable values and private credentials are never printed. Audit changes nothing.",
        "Railway, database data, authentication, frontend, Modal, and DNS are not modified.",
    ]
    return lines


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--operation", choices=["audit", "deploy_api"], default="audit")
    parser.add_argument("--data-verified", action="store_true")
    args = parser.parse_args()
    client = Coolify(
        os.getenv("COOLIFY_URL") or "https://coolify.gnsis.studio",
        os.getenv("COOLIFY_API_TOKEN", ""),
    )
    result = inventory(client, os.getenv("COOLIFY_APPLICATION_UUID") or "")
    issues = blockers(result, args.data_verified)
    report = "\n".join(summary_lines(result, issues)) + "\n"
    print(report)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as output:
            output.write(report)
    if args.operation == "audit":
        return 0
    if issues:
        print("API deployment blocked until all verification gates pass.", file=sys.stderr)
        return 2
    response = client.request(
        "POST", "/deploy?" + urllib.parse.urlencode({"uuid": result["app_uuid"]})
    )
    if not isinstance(response, dict) or not response.get("deployments"):
        raise ValueError("Coolify did not confirm a queued deployment.")
    print("API-only deployment queued. Public DNS cutover is NOT performed.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ValueError as error:
        print("ERROR: " + str(error), file=sys.stderr)
        sys.exit(1)
