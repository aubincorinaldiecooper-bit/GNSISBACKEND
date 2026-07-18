"""Thin server-side client for LiteLLM's key-management admin API.

Used ONLY by the backend to issue / revoke customer virtual keys, authenticated
with the LiteLLM **master key** — a credential that never leaves the server (no
browser, no agent, no run token ever sees it). It speaks a small, documented
slice of the LiteLLM proxy admin contract over the stdlib (no LiteLLM SDK):

* ``POST {litellm_url}/key/generate`` — mint a key with an optional budget.
* ``POST {litellm_url}/key/delete``   — revoke by token.
* ``GET  {litellm_url}/key/info``     — read a key's live spend/budget.

``_http_request`` is a module-level indirection so tests never touch the network.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal
from typing import Dict, List, Optional, Tuple


class VirtualKeyError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _http_request(
    method: str, url: str, headers: Dict[str, str], body: Optional[bytes] = None, timeout: int = 30
) -> Tuple[int, str]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        raise VirtualKeyError(f"could not reach LiteLLM admin: {exc.reason}", status=502) from exc


def _require_enabled(settings) -> None:
    if not settings.virtual_keys_enabled:
        raise VirtualKeyError("virtual keys are not configured", status=503)


def _headers(settings) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.litellm_master_key}",
        "Content-Type": "application/json",
        "User-Agent": "gnsis-admin",
    }


def _base(settings) -> str:
    return settings.litellm_url.rstrip("/")


def _parse(text: str) -> dict:
    try:
        return json.loads(text) if text else {}
    except json.JSONDecodeError:
        return {}


def generate_key(
    settings,
    *,
    key_alias: str,
    max_budget: Optional[str] = None,
    budget_duration: Optional[str] = None,
    models: Optional[List[str]] = None,
    metadata: Optional[dict] = None,
) -> Dict[str, object]:
    """Mint a LiteLLM virtual key. Returns the raw LiteLLM response dict.

    The response's ``key`` is the one-time secret; ``token`` is the durable
    (hashed) id GNSIS stores and later uses to revoke / inspect the key.
    """
    _require_enabled(settings)
    payload: Dict[str, object] = {}
    if key_alias:
        payload["key_alias"] = key_alias
    if max_budget is not None:
        # LiteLLM's wire format is a JSON number; our own ledger stays decimal.
        payload["max_budget"] = float(Decimal(str(max_budget)))
    if budget_duration:
        payload["budget_duration"] = budget_duration
    if models:
        payload["models"] = models
    if metadata:
        payload["metadata"] = metadata

    status, text = _http_request(
        "POST", f"{_base(settings)}/key/generate", _headers(settings),
        json.dumps(payload).encode("utf-8"),
    )
    data = _parse(text)
    if status >= 400:
        detail = data.get("error") if isinstance(data.get("error"), str) else None
        raise VirtualKeyError(f"LiteLLM rejected key creation: {detail or text[:200]}", status=502)
    if not data.get("key"):
        raise VirtualKeyError("LiteLLM did not return a key", status=502)
    return data


def delete_key(settings, token: str) -> None:
    _require_enabled(settings)
    status, text = _http_request(
        "POST", f"{_base(settings)}/key/delete", _headers(settings),
        json.dumps({"keys": [token]}).encode("utf-8"),
    )
    if status >= 400:
        raise VirtualKeyError(f"LiteLLM rejected key deletion: {text[:200]}", status=502)


def key_info(settings, token: str) -> dict:
    """Live key info (spend/budget). Returns ``{}`` if LiteLLM has no record."""
    _require_enabled(settings)
    q = urllib.parse.urlencode({"key": token})
    status, text = _http_request(
        "GET", f"{_base(settings)}/key/info?{q}", _headers(settings),
    )
    if status == 404:
        return {}
    if status >= 400:
        raise VirtualKeyError(f"LiteLLM rejected key info: {text[:200]}", status=502)
    data = _parse(text)
    info = data.get("info")
    return info if isinstance(info, dict) else data
