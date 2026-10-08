#!/usr/bin/env python3
"""Check the Railway project token and source environment without exposing secrets.

This preflight is read-only and never requests environment-variable values.
It retries safely when run on a schedule while the Railway dashboard/API is down.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ID = "3c4623dd-01c1-400e-ad94-0c15ea32fe8b"
ENVIRONMENT_ID = "20cfdf5c-39ca-47f2-8793-2daa72fa8fb2"
SERVICE_NAMES = ("meticulous-illumination", "GNSISWORKER", "GNSISBEAT",
                 "Postgres", "Redis", "GNSIS AUTH", "GNSISFRONTEND")


def cli(args, directory, environment):
    try:
        result = subprocess.run(["railway", *args], cwd=directory, env=environment,
                                text=True, capture_output=True, check=False, timeout=45)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("Railway CLI failed to start or timed out") from None
    if result.returncode:
        # Never display the CLI's raw stdout/stderr in CI; it may include details.
        raise RuntimeError("Railway CLI cannot reach the project (or token scope is invalid)")
    return result.stdout


def probe():
    token = os.getenv("RAILWAY_TOKEN") or ""
    if not token.strip():
        raise RuntimeError("GitHub secret RAILWAY_TOKEN is missing")
    env = os.environ.copy()
    env.pop("RAILWAY_API_TOKEN", None)
    with tempfile.TemporaryDirectory(prefix="gnsis-railway-probe-") as work:
        cli(["link", "--project", PROJECT_ID, "--environment", ENVIRONMENT_ID, "--json"],
            work, env)
        payload = json.loads(cli(["status", "--json"], work, env))
    if not isinstance(payload, dict):
        raise RuntimeError("Railway status returned an unexpected format")
    return [
        "## GNSIS Railway source preflight",
        "",
        "- Project token: authenticated (CLI link and status succeeded)",
        "- Project: fulfilling-wonder",
        "- Expected production services: " + ", ".join(SERVICE_NAMES),
        "- No service configurations or data were modified.",
        "- No Railway secret values were requested, displayed, or exported.",
        "",
        "Next: verified secret transfer and PostgreSQL export/restore are separate migration stages.",
    ]


def main():
    try:
        output = probe()
        success = True
    except (RuntimeError, ValueError, json.JSONDecodeError) as error:
        output = ["## GNSIS Railway source preflight", "",
                  "- Source connection not verified: " + str(error),
                  "- This may be temporary during a Railway outage.",
                  "- No database or deployment changes occurred."]
        success = False
    report = "\n".join(output) + "\n"
    print(report)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as handle:
            handle.write(report)
    if not success and os.getenv("PROBE_SCHEDULED") != "true":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
