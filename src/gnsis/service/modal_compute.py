"""Modal compute boundary owned by the GNSIS worker.

The Railway worker holds the Modal workspace token, exactly like GNSIS's worker
did.  GitHub Actions is CI only; it is not the production credential holder.

This module keeps the Modal SDK behind one small boundary so the rest of GNSIS
does not depend on Modal-specific APIs.  Runtime callers can discover the live
GNSIS web address and check its health, and the same for Ornith, the brain it
calls for tasks.  Deployment is an explicit operator action run by the worker,
never an automatic side effect of worker startup.
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
        live_app_name: str = "gnsis-live",
        live_function_name: str = "gnsis_live_server",
        ornith_app_name: str = "gnsis-ornith",
        ornith_function_name: str = "ornith_server_v2",
        modal_module: Optional[Any] = None,
    ) -> None:
        if not token_id or not token_secret:
            raise ValueError("Modal credentials are incomplete")
        self.token_id = token_id
        self.token_secret = token_secret
        self.ref = ModalRuntimeRef(
            app_name=live_app_name,
            function_name=live_function_name,
            environment=environment,
        )
        # The brain the runtime calls for tasks, deployed as its own app.
        self.ornith_ref = ModalRuntimeRef(
            app_name=ornith_app_name,
            function_name=ornith_function_name,
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

    def _web_url(self, ref: ModalRuntimeRef) -> str:
        """Return a deployed function's web address without starting a GPU."""
        # The Modal SDK reads credentials from the environment.  Set them only
        # around this call; never log or persist either value.
        previous_id = os.environ.get("MODAL_TOKEN_ID")
        previous_secret = os.environ.get("MODAL_TOKEN_SECRET")
        try:
            os.environ["MODAL_TOKEN_ID"] = self.token_id
            os.environ["MODAL_TOKEN_SECRET"] = self.token_secret
            fn = self._sdk().Function.from_name(
                ref.app_name,
                ref.function_name,
                environment_name=ref.environment,
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
            raise RuntimeError(f"{ref.app_name}/{ref.function_name} has no web address")
        return str(url).rstrip("/")

    def live_web_url(self) -> str:
        """Return the deployed GNSIS web-server URL without starting a GPU."""
        return self._web_url(self.ref)

    def ornith_web_url(self) -> str:
        """Return the deployed Ornith URL without starting a GPU.

        This is the address the live runtime's worker configuration needs.
        Nothing here asks Ornith anything: the key that would authenticate such
        a request lives in the Modal secret, not on this worker.
        """
        return self._web_url(self.ornith_ref)

    def live_health(self, *, timeout_seconds: float = 1800.0) -> dict[str, Any]:
        """Cold-start the live runtime and return its /health document."""
        url = self.live_web_url()
        try:
            with urllib.request.urlopen(
                f"{url}/health", timeout=timeout_seconds
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise RuntimeError(f"GNSIS health check failed: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            raise RuntimeError(f"GNSIS reported unhealthy status: {payload!r}")
        return payload

    def deploy_live(
        self,
        *,
        repo_root: str = "/app",
        models_volume: str = "gnsis-models",
    ) -> None:
        """Deploy the checked-in live runtime from the worker image.

        Deliberately explicit: callers choose when to deploy.  The worker does
        not mutate production infrastructure merely because it restarted.
        """
        env = self._credential_env()
        env["GNSIS_LIVE_MODELS_VOLUME"] = models_volume
        subprocess.run(
            [
                "modal",
                "deploy",
                "-e",
                self.ref.environment,
                "modal/live.py",
            ],
            cwd=repo_root,
            env=env,
            check=True,
        )

    def deploy_ornith(
        self,
        *,
        repo_root: str = "/app",
        cache_volume: str = "gnsis-ornith-cache",
        secret_name: str = "gnsis-ornith-auth",
        image: Optional[str] = None,
    ) -> None:
        """Deploy the brain the live runtime calls for tasks.

        Explicit, like the runtime's own deploy. The app name comes from this
        provider's Ornith reference, so a deploy goes where the status call
        looks and cannot land on some other app. Deploying does not point the
        runtime at the result: that is a configuration change and a redeploy of
        the runtime, described in docs/live_runtime.md.
        """
        env = self._credential_env()
        env["ORNITH_MODAL_APP_NAME"] = self.ornith_ref.app_name
        env["ORNITH_CACHE_VOLUME"] = cache_volume
        env["ORNITH_SECRET_NAME"] = secret_name
        if image:
            env["ORNITH_VLLM_IMAGE"] = image
        subprocess.run(
            [
                "modal",
                "deploy",
                "-e",
                self.ornith_ref.environment,
                "modal/ornith.py",
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
        live_app_name=settings.live_modal_app_name,
        live_function_name=settings.live_modal_function_name,
        # No function-name setting on purpose: a Modal function is named by its
        # `def`, so modal/ornith.py always publishes ornith_server_v2 and a
        # configurable lookup name could only ever point at nothing.
        ornith_app_name=getattr(settings, "ornith_modal_app_name", "gnsis-ornith"),
    )
