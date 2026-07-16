"""Cryptographic verification of GitHub Actions OIDC identities.

This is the trust boundary that lets a fresh, GitHub-hosted VM prove — without
any shared secret — that it is *the* fixed executor workflow, running the exact
audited commit, for *this* job. Verification is in two stages:

1. :class:`GithubOidcVerifier` checks the JWT itself: RS256 signature against
   GitHub's published JWKS (reusing the rate-limited :class:`JwksCache`, so key
   rotation is handled), the exact issuer and the exact custom audience, and the
   standard time claims. An unsigned or wrongly-signed token never gets past here.

2. :func:`check_execution_claims` checks the *contents* against configuration and
   the dispatched run record: repository, repository id, visibility, owner,
   event, ref, ref type, workflow_ref, workflow commit SHA, run id, run attempt
   and runner environment. The actor is deliberately **not** a trust boundary.

Any failure raises :class:`OidcError`, carrying a failure reason and category.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import jwt
from jwt import InvalidTokenError

from ..auth import JwksCache
from .models import FailureCategory

# GitHub Actions OIDC tokens are RS256. Never accept anything symmetric or "none".
ALLOWED_ALGORITHMS = ["RS256"]

GITHUB_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
GITHUB_OIDC_JWKS_URL = "https://token.actions.githubusercontent.com/.well-known/jwks"


class OidcError(Exception):
    """An OIDC token failed verification or claim validation."""

    def __init__(self, reason: str, category: str = FailureCategory.OIDC, status: int = 401):
        super().__init__(reason)
        self.reason = reason
        self.category = category
        self.status = status


class GithubOidcVerifier:
    """Verifies the JWT: signature, algorithm, issuer, audience, time claims."""

    def __init__(self, jwks: JwksCache, issuer: str, audience: str) -> None:
        self._jwks = jwks
        self._issuer = issuer
        self._audience = audience

    @classmethod
    def default(cls, audience: str, issuer: str = GITHUB_OIDC_ISSUER, fetcher=None) -> "GithubOidcVerifier":
        cache = JwksCache(url=None if fetcher else GITHUB_OIDC_JWKS_URL, fetcher=fetcher)
        return cls(cache, issuer=issuer, audience=audience)

    def verify(self, token: str) -> Dict[str, Any]:
        try:
            header = jwt.get_unverified_header(token)
        except InvalidTokenError as exc:
            raise OidcError(f"malformed token: {exc}") from exc

        alg = header.get("alg")
        if alg not in ALLOWED_ALGORITHMS:
            raise OidcError("unacceptable token algorithm")
        kid = header.get("kid")
        if not kid:
            raise OidcError("token missing key id")

        try:
            signing_key = self._jwks.get_key(kid)
        except Exception as exc:  # noqa: BLE001 - JwksCache raises AuthError
            raise OidcError(f"unknown signing key: {exc}") from exc

        try:
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=ALLOWED_ALGORITHMS,
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "iat", "aud", "iss", "sub"]},
            )
        except InvalidTokenError as exc:
            raise OidcError(f"invalid token: {exc}") from exc

        # Defense in depth: PyJWT already checked iss/aud, but assert exactly.
        if claims.get("iss") != self._issuer:
            raise OidcError("wrong issuer")
        return claims


def _require(claims: Dict[str, Any], key: str) -> Any:
    if key not in claims or claims[key] in (None, ""):
        raise OidcError(f"token missing claim: {key}")
    return claims[key]


def check_execution_claims(
    claims: Dict[str, Any],
    *,
    expected_repository: str,
    expected_owner: str,
    expected_repository_id: Optional[int],
    expected_workflow_ref: str,
    trusted_workflow_sha: str,
    expected_run_id: Optional[int],
    expected_run_attempt: Optional[int],
) -> None:
    """Validate the identity claims of a GitHub Actions OIDC token.

    ``expected_repository_id``/``expected_run_id``/``expected_run_attempt`` may be
    ``None`` when not yet resolved (first exchange), in which case that specific
    binding check is deferred to the caller — the repository *name*, owner,
    workflow_ref, workflow SHA, event, ref, ref type and runner environment are
    always enforced regardless.
    """
    # Repository name — necessary but explicitly NOT the sole trust boundary.
    repository = _require(claims, "repository")
    if repository != expected_repository:
        raise OidcError(f"wrong repository: {repository}")

    owner = _require(claims, "repository_owner")
    if owner != expected_owner:
        raise OidcError(f"wrong repository owner: {owner}")

    # Repository id: pin against the id resolved through the GitHub API. A repo
    # can be renamed; the numeric id cannot be spoofed by a rename/typosquat.
    if expected_repository_id is not None:
        claimed_id = _require(claims, "repository_id")
        if str(claimed_id) != str(expected_repository_id):
            raise OidcError(f"wrong repository id: {claimed_id}")

    # Private-only: a public executor claim is rejected outright.
    visibility = claims.get("repository_visibility")
    if visibility != "private":
        raise OidcError(f"repository is not private: {visibility}", category=FailureCategory.SECURITY)

    # Only a direct workflow_dispatch of the fixed workflow on the fixed branch.
    event = _require(claims, "event_name")
    if event != "workflow_dispatch":
        raise OidcError(f"wrong event: {event}")

    ref = _require(claims, "ref")
    expected_ref = expected_workflow_ref.split("@", 1)[1]  # refs/heads/<branch>
    if ref != expected_ref:
        raise OidcError(f"wrong ref: {ref}")

    ref_type = claims.get("ref_type")
    if ref_type not in (None, "branch"):
        raise OidcError(f"wrong ref type: {ref_type}")

    # workflow_ref must be exactly our workflow file at our branch — this is what
    # rejects forks, other branches, other workflow files and reusable-workflow
    # callers (their workflow_ref/job_workflow_ref would differ).
    workflow_ref = _require(claims, "workflow_ref")
    if workflow_ref != expected_workflow_ref:
        raise OidcError(f"wrong workflow_ref: {workflow_ref}")
    job_workflow_ref = claims.get("job_workflow_ref")
    if job_workflow_ref is not None and job_workflow_ref != expected_workflow_ref:
        raise OidcError(f"wrong job_workflow_ref: {job_workflow_ref}")

    # The workflow commit SHA must equal the exact trusted, audited commit.
    workflow_sha = claims.get("job_workflow_sha") or claims.get("sha")
    if not workflow_sha:
        raise OidcError("token missing workflow sha")
    if workflow_sha != trusted_workflow_sha:
        raise OidcError(
            f"wrong workflow sha: {workflow_sha}", category=FailureCategory.SECURITY
        )

    # GitHub-hosted runners only — never a self-hosted runner.
    runner_env = claims.get("runner_environment")
    if runner_env != "github-hosted":
        raise OidcError(
            f"non-github-hosted runner: {runner_env}", category=FailureCategory.SECURITY
        )

    # Bind to the exact dispatched run and current attempt when known.
    if expected_run_id is not None:
        claimed_run = _require(claims, "run_id")
        if str(claimed_run) != str(expected_run_id):
            raise OidcError(f"wrong run id: {claimed_run}")
    if expected_run_attempt is not None:
        claimed_attempt = _require(claims, "run_attempt")
        if str(claimed_attempt) != str(expected_run_attempt):
            raise OidcError(
                f"wrong run attempt: {claimed_attempt}", category=FailureCategory.STALE_ATTEMPT
            )
