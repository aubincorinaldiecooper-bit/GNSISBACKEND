"""Environment-driven configuration for the Railway services.

Both the FastAPI API and the Celery worker read the same settings from the
environment, so the two Railway services stay in lockstep. Nothing here imports a
heavy dependency, so it can be inspected (and unit-tested) on its own.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional


def _normalize_db_url(url: str) -> str:
    """Make a Railway/Heroku-style URL explicit about the psycopg driver.

    SQLAlchemy 2.x wants ``postgresql+psycopg://``; platforms commonly hand out
    ``postgres://`` or ``postgresql://``.
    """
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


@dataclass
class Settings:
    database_url: str
    redis_url: str
    anthropic_api_key: Optional[str] = None
    openrouter_api_key: Optional[str] = None

    # GitHub App — the platform-owned credentials. The App id + private key are
    # used to mint short-lived installation tokens per run. The global
    # installation id is DEPRECATED for user runs (each run now resolves its own
    # installation) and kept only as an optional fallback for legacy/internal runs.
    github_app_id: Optional[str] = None
    github_app_private_key: Optional[str] = None
    github_app_installation_id: Optional[str] = None
    github_app_slug: Optional[str] = None
    github_webhook_secret: Optional[str] = None

    # Better Auth bridge — how the FastAPI backend authenticates end users.
    # The backend verifies short-lived JWTs minted by the Better Auth service
    # against its published JWKS; it never sees Better Auth secrets or cookies.
    better_auth_jwks_url: Optional[str] = None
    better_auth_issuer: Optional[str] = None
    better_auth_audience: Optional[str] = None

    # Server-to-server channel to the auth service (installation ownership check).
    auth_internal_url: Optional[str] = None
    auth_internal_secret: Optional[str] = None

    # The user-facing frontend origin (CORS + webhook/onboarding return URLs).
    frontend_url: Optional[str] = None

    default_engine: str = "claude"
    default_base_branch: str = "main"
    workspace_root: str = "/tmp/gnsis-workspaces"
    api_key: Optional[str] = None  # optional shared secret to protect the API

    allowed_repos: List[str] = field(default_factory=list)

    # CORS origins allowed to call the API from a browser. Default "*".
    cors_origins: List[str] = field(default_factory=lambda: ["*"])

    # Long-term memory backend: "postgres" (default) or "none".
    memory_backend: str = "postgres"

    # Sandbox for executing model-written code: "none" (run in the worker
    # container) or "docker" (ephemeral, isolated container per job).
    sandbox: str = "none"
    sandbox_image: str = "gnsis-sandbox:latest"
    sandbox_network: str = "bridge"
    sandbox_memory: str = "2g"
    sandbox_cpus: str = "2"
    sandbox_timeout: int = 1800

    @property
    def user_auth_enabled(self) -> bool:
        """True when Better Auth JWT verification is fully configured."""
        return bool(
            self.better_auth_jwks_url
            and self.better_auth_issuer
            and self.better_auth_audience
        )

    @property
    def installation_verification_enabled(self) -> bool:
        """True when the backend can call the auth service to verify ownership."""
        return bool(self.auth_internal_url and self.auth_internal_secret)

    def missing_production_vars(self) -> List[str]:
        """Names of required-for-production settings that are absent.

        Used by the startup check to fail loudly and actionably rather than
        limping along and rejecting every user request at runtime.
        """
        missing: List[str] = []
        if not self.user_auth_enabled:
            for name, value in (
                ("BETTER_AUTH_JWKS_URL", self.better_auth_jwks_url),
                ("BETTER_AUTH_ISSUER", self.better_auth_issuer),
                ("BETTER_AUTH_AUDIENCE", self.better_auth_audience),
            ):
                if not value:
                    missing.append(name)
        if not self.installation_verification_enabled:
            for name, value in (
                ("GNSIS_AUTH_INTERNAL_URL", self.auth_internal_url),
                ("GNSIS_AUTH_INTERNAL_SECRET", self.auth_internal_secret),
            ):
                if not value:
                    missing.append(name)
        for name, value in (
            ("GITHUB_APP_ID", self.github_app_id),
            ("GITHUB_APP_PRIVATE_KEY", self.github_app_private_key),
            ("GITHUB_APP_SLUG", self.github_app_slug),
        ):
            if not value:
                missing.append(name)
        return missing

    @property
    def celery_broker_url(self) -> str:
        return os.environ.get("CELERY_BROKER_URL", self.redis_url)

    @property
    def celery_result_backend(self) -> str:
        return os.environ.get("CELERY_RESULT_BACKEND", self.redis_url)

    @classmethod
    def from_env(cls) -> "Settings":
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            raise RuntimeError("DATABASE_URL is required")
        redis_url = os.environ.get("REDIS_URL")
        if not redis_url:
            raise RuntimeError("REDIS_URL is required")
        repos = [
            r.strip()
            for r in os.environ.get("GNSIS_ALLOWED_REPOS", "").split(",")
            if r.strip()
        ]
        cors = [
            o.strip()
            for o in os.environ.get("GNSIS_CORS_ORIGINS", "*").split(",")
            if o.strip()
        ] or ["*"]
        return cls(
            database_url=_normalize_db_url(database_url),
            redis_url=redis_url,
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
            openrouter_api_key=os.environ.get("OPENROUTER_API_KEY"),
            github_app_id=os.environ.get("GITHUB_APP_ID"),
            github_app_private_key=os.environ.get("GITHUB_APP_PRIVATE_KEY"),
            github_app_installation_id=os.environ.get("GITHUB_APP_INSTALLATION_ID"),
            github_app_slug=os.environ.get("GITHUB_APP_SLUG"),
            github_webhook_secret=os.environ.get("GITHUB_WEBHOOK_SECRET"),
            better_auth_jwks_url=os.environ.get("BETTER_AUTH_JWKS_URL"),
            better_auth_issuer=os.environ.get("BETTER_AUTH_ISSUER"),
            better_auth_audience=os.environ.get("BETTER_AUTH_AUDIENCE"),
            auth_internal_url=os.environ.get("GNSIS_AUTH_INTERNAL_URL"),
            auth_internal_secret=os.environ.get("GNSIS_AUTH_INTERNAL_SECRET"),
            frontend_url=os.environ.get("GNSIS_FRONTEND_URL"),
            default_engine=os.environ.get("GNSIS_DEFAULT_ENGINE", "claude"),
            default_base_branch=os.environ.get("GNSIS_DEFAULT_BASE_BRANCH", "main"),
            workspace_root=os.environ.get("GNSIS_WORKSPACE_ROOT", "/tmp/gnsis-workspaces"),
            api_key=os.environ.get("GNSIS_API_KEY"),
            allowed_repos=repos,
            cors_origins=cors,
            memory_backend=os.environ.get("GNSIS_MEMORY", "postgres"),
            sandbox=os.environ.get("GNSIS_SANDBOX", "none"),
            sandbox_image=os.environ.get("GNSIS_SANDBOX_IMAGE", "gnsis-sandbox:latest"),
            sandbox_network=os.environ.get("GNSIS_SANDBOX_NETWORK", "bridge"),
            sandbox_memory=os.environ.get("GNSIS_SANDBOX_MEMORY", "2g"),
            sandbox_cpus=os.environ.get("GNSIS_SANDBOX_CPUS", "2"),
            sandbox_timeout=int(os.environ.get("GNSIS_SANDBOX_TIMEOUT", "1800")),
        )


_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """Process-wide settings singleton, loaded lazily from the environment."""
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings
