#!/usr/bin/env python3
"""Verify the Railway project token through the Public GraphQL API.

Read-only. The token and variable values never enter logs, artifacts, or output.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ENDPOINT = "https://backboard.railway.com/graphql/v2"
PROJECT_ID = "3c4623dd-01c1-400e-ad94-0c15ea32fe8b"
ENVIRONMENT_ID = "20cfdf5c-39ca-47f2-8793-2daa72fa8fb2"
SERVICES = {
    "API": "9626dcfd-b7d8-4681-9407-1a1df5096f69",
    "Worker": "d3d00378-63ee-44de-8d04-909b1feb059a",
    "Beat": "e39c897a-fcf3-45e6-b050-6aa51a82d1c6",
    "Postgres": "bd2b6bc0-9111-4e53-9710-48eb448ada25",
    "Redis": "be4cec16-8e2d-4f5e-975a-0a4d35c4a98c",
    "Auth": "563f6a57-9cc7-45fc-9f11-05243abdac00",
    "Frontend": "20f192a9-8b9d-4a68-9d3d-93520c1702b2",
}


def graphql(token: str, query: str, variables: dict | None = None) -> dict:
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    request = urllib.request.Request(
        ENDPOINT,
        data=body,
        method="POST",
        headers={
            "Project-Access-Token": token,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "gnsis-railway-migration/1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read(2_000_000))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        raise RuntimeError("Railway Public API is unreachable or returned invalid data") from None
    if payload.get("errors"):
        # Never print GraphQL error bodies because implementations can echo request context.
        raise RuntimeError("Railway rejected the project token or query")
    return payload.get("data") or {}


def probe() -> list[str]:
    token = os.getenv("RAILWAY_TOKEN", "").strip()
    if not token:
        raise RuntimeError("GitHub secret RAILWAY_TOKEN is missing")

    token_info = graphql(
        token,
        "query { projectToken { projectId environmentId } }",
    ).get("projectToken") or {}

    if token_info.get("projectId") != PROJECT_ID or token_info.get("environmentId") != ENVIRONMENT_ID:
        raise RuntimeError("RAILWAY_TOKEN is valid but scoped to a different project/environment")

    query = """
    query variables($projectId: String!, $environmentId: String!, $serviceId: String) {
      variables(projectId: $projectId, environmentId: $environmentId, serviceId: $serviceId)
    }
    """
    counts: dict[str, int] = {}
    for name, service_id in SERVICES.items():
        variables = graphql(
            token,
            query,
            {"projectId": PROJECT_ID, "environmentId": ENVIRONMENT_ID, "serviceId": service_id},
        ).get("variables") or {}
        if not isinstance(variables, dict):
            raise RuntimeError("Railway returned an unexpected variable payload")
        counts[name] = len(variables)

    return [
        "## GNSIS Railway source preflight",
        "",
        "- Project token: authenticated",
        "- Token scope: correct GNSIS production project/environment",
        "- Services readable: " + str(len(counts)) + "/" + str(len(SERVICES)),
        "- Variable counts: " + ", ".join(name + "=" + str(counts[name]) for name in SERVICES),
        "",
        "No variable values were printed, persisted, or uploaded.",
        "No Railway or Coolify resources were modified.",
        "",
        "Next migration stage: transfer only the verified configuration into Coolify, then perform PostgreSQL backup/restore and reconciliation before cutover.",
    ]


def main() -> int:
    try:
        report = probe()
    except RuntimeError as error:
        report = [
            "## GNSIS Railway source preflight",
            "",
            "- Source connection not verified: " + str(error),
            "- No database, deployment, or configuration changes occurred.",
        ]
        success = False
    else:
        success = True

    output = "\n".join(report) + "\n"
    print(output)
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as handle:
            handle.write(output)

    # Scheduled checks stay green while Railway is unavailable so they can retry.
    if not success and os.getenv("PROBE_SCHEDULED") == "true":
        return 0
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
