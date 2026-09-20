"""Modal compute boundary owned by the GNSIS worker.

The Railway worker holds the Modal workspace token, exactly like Clipit's worker
did.  GitHub Actions is CI only; it is not the production credential holder.

This module keeps the Modal SDK behind one small boundary so the rest of GNSIS
does not depend on Modal-specific APIs.  Runtime callers can discover the live
Gander web address and check its health.  Deployment is an explicit operator
action run by the worker, never an automatic side effect of worker startup.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class ModalRuntimeRef:
    app_name: str
    function_name: str
    environment: str


class ModalCompute:
    """Small provider boundary for GNSIS-owned Modal compute."""

    def __init__(
        self,
        *,
        token_id: str,
        token_secret: str,
        environment: str = "main",
        gander_app_name: str = "gnsis-live",
        gander_function_name: str = "gander_server",
        modal_module: Optional[Any] = None,
    ) -> None:
        if not token_id or not token_secret:
            raise ValueError("Modal credentials are incomplete")
        self.token_id = token_id
        self.token_secret = token_secret
        self.ref = ModalRuntimeRef(
            app_name=gander_app_name,
            function_name=gander_function_name,
            environment=environment,
        )
        self._modal = modal_module

    def _sdk(self):
        if self._modal is None:
            import modal  # lazy: API/beat processes do not need the SDK

            self._modal = modal
        return self._modal

    def _credential_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["MODAL_TOKEN_ID"] = self.token_id
        env["MODAL_TOKEN_SECRET"] = self.token_secret
        env["MODAL_ENVIRONMENT"] = self.ref.environment
        return env

    def gander_web_url(self) -> str:
        """Return the deployed Gander web-server URL without starting a GPU."""
        # The Modal SDK reads credentials from the environment.  Set them only
        # around this call; never log or persist either value.
        previous_id = os.environ.get("MODAL_TOKEN_ID")
        previous_secret = os.environ.get("MODAL_TOKEN_SECRET")
        try:
            os.environ["MODAL_TOKEN_ID"] = self.token_id
            os.environ["MODAL_TOKEN_SECRET"] = self.token_secret
            fn = self._sdk().Function.from_name(
                self.ref.app_name,
                self.ref.function_name,
                environment_name=self.ref.environment,
            )
            fn.hydrate()
            url = fn.get_web_url()
        finally:
            if previous_id is None:
                os.environ.pop("MODAL_TOKEN_ID", None)
            else:
                os.environ["MODAL_TOKEN_ID"] = previous_id
            if previous_secret is None:
                os.environ.pop("MODAL_TOKEN_SECRET", None)
            else:
                os.environ["MODAL_TOKEN_SECRET"] = previous_secret

        if not url:
            raise RuntimeError(
                f"{self.ref.app_name}/{self.ref.function_name} has no web address"
            )
        return str(url).rstrip("/")

    def gander_health(self, *, timeout_seconds: float = 1800.0) -> dict[str, Any]:
        """Cold-start the live runtime and return its /health document."""
        url = self.gander_web_url()
        try:
            with urllib.request.urlopen(
                f"{url}/health", timeout=timeout_seconds
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise RuntimeError(f"Gander health check failed: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            raise RuntimeError(f"Gander reported unhealthy status: {payload!r}")
        return payload

    def deploy_gander(
        self,
        *,
        repo_root: str = "/app",
        models_volume: str = "clipit-gander-weights",
        secret_name: str = "clipit-gander-ornith",
    ) -> None:
        """Deploy the checked-in live runtime from the worker image.

        Deliberately explicit: callers choose when to deploy.  The worker does
        not mutate production infrastructure merely because it restarted.
        """
        env = self._credential_env()
        env["GANDER_MODELS_VOLUME"] = models_volume
        env["GANDER_SECRET_NAME"] = secret_name
        subprocess.run(
            [
                "modal",
                "deploy",
                "-e",
                self.ref.environment,
                "modal/gander.py",
            ],
            cwd=repo_root,
            env=env,
            check=True,
        )


def from_settings(settings) -> ModalCompute:
    """Build the provider from the worker's process settings."""
    if not settings.modal_token_id or not settings.modal_token_secret:
        raise RuntimeError("GNSISWORKER is missing Modal credentials")
    return ModalCompute(
        token_id=settings.modal_token_id,
        token_secret=settings.modal_token_secret,
        environment=settings.modal_environment,
        gander_app_name=settings.gander_modal_app_name,
        gander_function_name=settings.gander_modal_function_name,
    )
