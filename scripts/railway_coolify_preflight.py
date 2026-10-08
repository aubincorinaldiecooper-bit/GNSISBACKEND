#!/usr/bin/env python3
"""Read-only Railway CLI token preflight for the GNSIS Coolify migration.

Nothing is written to Railway or Coolify. Service variable *values* are read
into process memory solely to establish availability. No raw CLI output, API
bodies, or secret values are printed, persisted, or attached as artifacts.
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
SERVICES = {
    "API": "9626dcfd-b7d8-4681-9407-1a1df5096f69",
    "Worker": "d3d00378-63ee-44de-8d04-909b1feb059a",
    "Beat": "e39c897a-fcf3-45e6-b050-6aa51a82d1c6",
    "Postgres": "bd2b6bc0-9111-4e53-9710-48eb448ada25",
    "Redis": "be4cec16-8e2d-4f5e-975a-0a4d35c4a98c",
    "Auth": "563f6a57-9cc7-45fc-9f11-05243abdac00",
    "Frontend": "20f192a9-8b9d-4a68-9d3d-93520c1702b2",
}
REQUIRED = {
    "API": {"DATABASE_URL", "REDIS_URL", "GITHUB_APP_PRIVATE_KEY", "GNSIS_API_KEY",
            "GNSIS_AUTH_INTERNAL_SECRET", "OPENROUTER_API_KEY",
            "GNSIS_VIRTUAL_KEY_PEPPER"},
    "Worker": {"DATABASE_URL", "REDIS_URL", "MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET",
               "GITHUB_APP_PRIVATE_KEY", "GNSIS_EDGE_SECRET"},
    "Beat": {"DATABASE_URL", "REDIS_URL"},
    "Postgres": {"POSTGRES_PASSWORD", "DATABASE_URL"},
    "Redis": {"REDIS_URL", "REDIS_PASSWORD"},
    "Auth": {"AUTH_DATABASE_URL", "BETTER_AUTH_SECRET", "RESEND_API_KEY",
             "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"},
    "Frontend": {"GNSIS_EDGE_SECRET", "MODAL_PROXY_KEY", "MODAL_PROXY_SECRET"},
}


def parse_variables(raw: str) -> dict:
    data = json.loads(raw)
    if isinstance(data, dict) and isinstance(data.get("variables"), dict):
        data = data["variables"]
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        data = data["data"]
    if not isinstance(data, dict):
        raise ValueError("Unexpected Railway variables JSON format")
    return data


def exec_cli(args: list[str], cwd: str, env: dict) -> str:
    try:
        result = subprocess.run(["railway", *args], cwd=cwd, env=env,
                                check=False, capture_output=True, text=True, timeout=45)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise RuntimeError("Railway CLI unavailable (timeout or startup error)") from None
    if result.returncode != 0:
        # Do not leak stderr. Some CLIs echo flags/variables in error messages.
        raise RuntimeError("Railway CLI request failed (connection, auth, or service unavailable)")
    return result.stdout


def inspect(token: str) -> list[str]:
    if not token.strip():
        raise RuntimeError("Missing RAILWAY_TOKEN GitHub Actions secret")
    if not (os.getenv("COOLIFY_API_TOKEN") or "").strip():
        raise RuntimeError("Missing COOLIFY_API_TOKEN GitHub Actions secret")
    env = dict(os.environ)
    env["RAILWAY_TOKEN"] = token
    env.pop("RAILWAY_API_TOKEN", None)
    report = ["## Railway → Coolify source access", "",
              "Railway CLI project token detected. Verifying production service variables.",
              "Variable values are never logged.", ""]
    with tempfile.TemporaryDirectory(prefix="gnsis-railway-probe-") as tmpdir:
        exec_cli(["link", "--project", PROJECT_ID, "--environment", ENVIRONMENT_ID, "--json"],
                 tmpdir, env)
        for name, service_id in SERVICES.items():
            raw = exec_cli(["variable", "list", "--service", service_id,
                            "--environment", ENVIRONMENT_ID, "--json"], tmpdir, env)
            variables = parse_variables(raw)
            missing = sorted(REQUIRED[name] - variables.keys())
            report.append("- " + name + ": "
                          + ("required variables located" if not missing
                             else "missing: " + ", ".join(missing)))
    report.extend(["", "No Railway or Coolify data/configuration was modified.",
                   "This check does not export database rows or move secrets."])
    return report


def main():
    try:
        report = inspect(os.getenv("RAILWAY_TOKEN") or "")
        result = 0
    except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
        # The messages above deliberately include neither variable values nor CLI output.
        report = ["## Railway → Coolify source access", "",
                  "Source access not confirmed: " + str(exc),
                  "", "Railway may be temporarily unreachable, or token scope may need correction.",
                  "No migrations or changes attempted."]
        result = 1
    message = "\n".join(report) + "\n"
    print(message)
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as fp:
            fp.write(message)
    # Cron probes are expected to retry during an outage; avoid noisy failed runs.
    if os.getenv("PROBE_SCHEDULED") == "true":
        return 0
    return result


if __name__ == "__main__":
    sys.exit(main())
