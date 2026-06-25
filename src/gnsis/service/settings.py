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

    # GitHub App — used only by the publish step (worker), never the API.
    github_app_id: Optional[str] = None
    github_app_private_key: Optional[str] = None
    github_app_installation_id: Optional[str] = None

    default_engine: str = "claude"
    default_base_branch: str = "main"
    workspace_root: str = "/tmp/gnsis-workspaces"
    api_key: Optional[str] = None  # optional shared secret to protect the API

    allowed_repos: List[str] = field(default_factory=list)

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
        return cls(
            database_url=_normalize_db_url(database_url),
            redis_url=redis_url,
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
            openrouter_api_key=os.environ.get("OPENROUTER_API_KEY"),
            github_app_id=os.environ.get("GITHUB_APP_ID"),
            github_app_private_key=os.environ.get("GITHUB_APP_PRIVATE_KEY"),
            github_app_installation_id=os.environ.get("GITHUB_APP_INSTALLATION_ID"),
            default_engine=os.environ.get("GNSIS_DEFAULT_ENGINE", "claude"),
            default_base_branch=os.environ.get("GNSIS_DEFAULT_BASE_BRANCH", "main"),
            workspace_root=os.environ.get("GNSIS_WORKSPACE_ROOT", "/tmp/gnsis-workspaces"),
            api_key=os.environ.get("GNSIS_API_KEY"),
            allowed_repos=repos,
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
