"""Server-to-server client for the Better Auth service's internal endpoints.

The only cross-service call the backend makes to the auth service: verifying
that a GitHub App installation is actually accessible to the authenticated
GitHub user, before the backend claims it under that user's workspace. The auth
service holds the user's GitHub OAuth token (the backend never sees it) and asks
GitHub which installations that user can access.

The transport is injectable so this is unit-testable without a live auth
service — tests pass a fake ``poster`` that returns canned responses.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

# (url, headers, json_body) -> (status_code, parsed_json)
Poster = Callable[[str, Dict[str, str], Dict[str, Any]], "tuple[int, dict]"]


class InstallationVerificationError(Exception):
    """The auth service did not confirm the installation belongs to the user."""

    def __init__(self, message: str, status: int = 403) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class VerifiedInstallation:
    installation_id: int
    account_id: Optional[int]
    account_login: Optional[str]
    account_type: Optional[str]


def _http_post(url: str, headers: Dict[str, str], body: Dict[str, Any]) -> "tuple[int, dict]":
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for key, value in headers.items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            parsed = json.loads(exc.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            parsed = {}
        return exc.code, parsed


class AuthServiceClient:
    def __init__(
        self,
        base_url: str,
        internal_secret: str,
        poster: Optional[Poster] = None,
    ) -> None:
        if not (base_url and internal_secret):
            raise ValueError("auth service base_url and internal_secret are required")
        self._base_url = base_url.rstrip("/")
        self._secret = internal_secret
        self._post = poster or _http_post

    def verify_installation(
        self, auth_subject: str, installation_id: int
    ) -> VerifiedInstallation:
        """Confirm ``installation_id`` is accessible to ``auth_subject``.

        Raises :class:`InstallationVerificationError` on any non-confirmation —
        wrong internal credential, unknown user, no linked GitHub account,
        installation not accessible, or GitHub rejecting the token.
        """
        url = f"{self._base_url}/internal/github/verify-installation"
        headers = {"Authorization": f"Bearer {self._secret}"}
        body = {"auth_subject": auth_subject, "installation_id": installation_id}
        status, data = self._post(url, headers, body)

        if status != 200 or not isinstance(data, dict) or not data.get("valid"):
            # Deliberately generic upward; the auth service logs specifics.
            raise InstallationVerificationError(
                "installation is not accessible to this user", status=403
            )
        account = data.get("account") or {}
        return VerifiedInstallation(
            installation_id=int(data.get("installation_id", installation_id)),
            account_id=account.get("id"),
            account_login=account.get("login"),
            account_type=account.get("type"),
        )
