"""Environment-driven configuration for the Railway services.

Both the FastAPI API and the Celery worker read the same settings from the
environment, so the two Railway services stay in lockstep. Nothing here imports a
heavy dependency, so it can be inspected (and unit-tested) on its own.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import List, Optional

#: The single permitted execution environment for user coding jobs. Every user
#: job runs remotely in a fixed GitHub Actions workflow inside the private
#: executor repository. There is deliberately no other accepted value: no
#: ``local``, ``docker``, ``none``, ``daytona`` or Celery-in-process path.
EXECUTION_PROVIDER_GITHUB_ACTIONS = "github_actions"

#: Models a run may call through the gateway when none is configured explicitly.
DEFAULT_ALLOWED_MODELS: List[str] = ["anthropic/claude-opus-4.8"]


def _parse_json_env(name: str) -> dict:
    """Parse an optional JSON-object env var; return {} when unset/invalid."""
    raw = os.environ.get(name)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


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

    # Legacy Docker sandbox knobs. RETAINED ONLY for designing the hardened
    # container command inside GitHub Actions and for explicitly isolated tests.
    # They are NOT read on the user-job path anymore (see ``tasks.run_job``): user
    # jobs execute exclusively through the GitHub Actions executor.
    sandbox: str = "none"
    sandbox_image: str = "gnsis-sandbox:latest"
    sandbox_network: str = "bridge"
    sandbox_memory: str = "2g"
    sandbox_cpus: str = "2"
    sandbox_timeout: int = 1800

    # -- public-beta remote execution (GitHub Actions) ------------------------
    # The provider is fixed by configuration and is NEVER read from an API/frontend
    # job input. A missing or non-"github_actions" value blocks job creation.
    execution_provider: Optional[str] = None
    # The public backend URL the executor VM calls back to (OIDC, spec, source,
    # model gateway, events). Handed to the workflow via dispatch, not a secret.
    public_api_url: Optional[str] = None

    # Executor repository identity. Kept fully config-driven so the exact OIDC
    # ``repository`` / ``workflow_ref`` the backend trusts is operator-controlled.
    executor_owner: Optional[str] = None
    executor_repo: Optional[str] = None
    executor_workflow: str = "execute.yml"
    executor_ref: str = "main"
    executor_oidc_issuer: str = "https://token.actions.githubusercontent.com"
    executor_oidc_audience: Optional[str] = None
    # The exact commit SHA of the executor's default branch that is trusted to
    # run. The OIDC ``job_workflow_sha``/``sha`` must equal this at exchange time.
    executor_trusted_workflow_sha: Optional[str] = None

    # Time-to-live / deadlines (seconds).
    executor_token_ttl_seconds: int = 1800
    run_token_ttl_seconds: int = 1800
    executor_timeout_seconds: int = 1800

    # Callback / source / patch / event size ceilings (bytes).
    executor_source_max_bytes: int = 262_144_000
    executor_callback_max_bytes: int = 10_485_760
    executor_patch_max_bytes: int = 5_242_880
    executor_event_max_bytes: int = 5_242_880

    # Per-run model budgets, enforced by the gateway and the store.
    run_max_model_calls: int = 50
    run_max_input_tokens: int = 500_000
    run_max_output_tokens: int = 100_000
    run_max_cost_usd: float = 3.00
    # Server-controlled allowlist of models a run may invoke. A user prompt or a
    # repository file can never widen this.
    run_allowed_models: List[str] = field(
        default_factory=lambda: list(DEFAULT_ALLOWED_MODELS)
    )
    # Optional operator-supplied display metadata for the user-facing model
    # catalog, keyed by model id: {label, description, provider, speed_tier,
    # cost_tier, context_window}. A model without an entry falls back to its id.
    # The catalog NEVER lists a model that is not in run_allowed_models.
    model_metadata: dict = field(default_factory=dict)

    # Which Railway service this process is: "api", "worker" or "beat". Drives which
    # settings are required at startup. Defaults to "api". Production must run
    # exactly one beat replica; never use celery worker -B.
    service_role: str = "api"

    # -- LiteLLM metering (separate service; never embedded in this process) ---
    # When configured, native model requests route GNSIS -> LiteLLM -> provider,
    # and LiteLLM reports measured usage back to the callback below. When unset,
    # the gateway keeps its current direct OpenRouter path unchanged.
    litellm_url: Optional[str] = None          # LiteLLM proxy base URL (OpenAI-compatible)
    litellm_api_key: Optional[str] = None      # key GNSIS uses to call LiteLLM
    litellm_callback_secret: Optional[str] = None  # shared secret LiteLLM sends to the callback

    @property
    def litellm_enabled(self) -> bool:
        return bool(self.litellm_url and self.litellm_api_key)

    # -- billing (markup + prepaid balance + Stripe refills) ------------------
    # The markup is config-driven and versioned; it is never hardcoded per route.
    # Every completed charge stores the exact rate it applied (see billing.py), so
    # changing this later does not alter historical charges.
    markup_rate: str = "0.05"                 # decimal string, e.g. 0.05 = 5%
    rate_card_version: str = "beta-2026-07"
    default_currency: str = "USD"
    stripe_webhook_secret: Optional[str] = None  # whsec_... — verifies Stripe webhooks
    # Estimated dollar hold placed before a native model request when a balance
    # exists, released/settled by the usage callback. Keeps concurrent requests
    # from overspending before the actual cost is known.
    balance_reserve_estimate_usd: str = "0.05"

    # Hard ceiling on a single operator beta-credit grant (defence against a fat
    # finger). A grant above this is rejected. Not a per-workspace lifetime cap.
    beta_credit_max_usd: str = "50.00"

    # -- automatic welcome credit --------------------------------------------
    # A one-time credit granted to a workspace after its first verified GitHub
    # App claim. Idempotent per (workspace, campaign) — retries, reconnections,
    # and second installations never double-grant. Distinct from operator beta
    # grants: those are manual & per-request, this is user-triggered but fully
    # server-controlled.
    welcome_credit_enabled: bool = False
    # Total dollars granted per eligible workspace. Never exceeds
    # ``beta_credit_max_usd`` (the shared ceiling).
    welcome_credit_usd: str = "5.00"
    # Advertised per-run cap for welcome-credit-funded runs. The effective per-run
    # cost cap is still ``run_max_cost_usd`` — this value documents the SLA and
    # must not exceed it, so an operator can't promise more than the platform
    # will actually allow.
    welcome_credit_per_run_usd: str = "0.50"
    # Campaign identifier included in the grant reason + idempotency key. A new
    # campaign lets past-eligible workspaces receive a fresh credit; changing it
    # is deliberate operator action, not a side effect of a bug.
    welcome_credit_campaign: str = "beta-2026-07"
    # Platform-wide daily provider-spend ceiling. Sum of welcome grants over the
    # last 24 hours; further grants are silently skipped when reaching it (never
    # errored — the GitHub claim itself must always succeed). Empty = no ceiling.
    platform_daily_provider_limit_usd: str = ""

    # Optional server-side pepper mixed into the Genesis virtual-key hash. Keys are
    # high-entropy so plain SHA-256 is sufficient; a pepper adds defence-in-depth
    # if the key_hash column ever leaks. Rotating it invalidates existing keys.
    virtual_key_pepper: str = ""

    @property
    def billing_enabled(self) -> bool:
        """Prepaid balance enforcement is active once a Stripe webhook secret is set."""
        return bool(self.stripe_webhook_secret)

    # -- provider / executor configuration state -----------------------------
    @property
    def execution_provider_valid(self) -> bool:
        """True only when the fixed GitHub Actions provider is configured."""
        return self.execution_provider == EXECUTION_PROVIDER_GITHUB_ACTIONS

    @property
    def is_production(self) -> bool:
        """Heuristic: a Postgres DATABASE_URL means a real deployment.

        Tests and local dev use SQLite; production uses Postgres. Startup checks
        fail hard in production and only warn in dev, so the suite can boot the
        app without a full production configuration.
        """
        return self.database_url.startswith("postgresql")

    @property
    def executor_full_name(self) -> Optional[str]:
        if self.executor_owner and self.executor_repo:
            return f"{self.executor_owner}/{self.executor_repo}"
        return None

    @property
    def executor_ref_full(self) -> str:
        return f"refs/heads/{self.executor_ref}"

    @property
    def expected_workflow_ref(self) -> Optional[str]:
        """The exact OIDC ``job_workflow_ref`` the executor token must carry."""
        full = self.executor_full_name
        if not full:
            return None
        return f"{full}/.github/workflows/{self.executor_workflow}@refs/heads/{self.executor_ref}"

    def missing_execution_vars(self) -> List[str]:
        """Names of required public-beta execution settings that are absent/invalid.

        A single source of truth for "can this deployment run user jobs at all".
        """
        missing: List[str] = []
        if not self.execution_provider_valid:
            missing.append("GNSIS_EXECUTION_PROVIDER=github_actions")
        for name, value in (
            ("GNSIS_PUBLIC_API_URL", self.public_api_url),
            ("GNSIS_EXECUTOR_OWNER", self.executor_owner),
            ("GNSIS_EXECUTOR_REPO", self.executor_repo),
            ("GNSIS_EXECUTOR_WORKFLOW", self.executor_workflow),
            ("GNSIS_EXECUTOR_REF", self.executor_ref),
            ("GNSIS_EXECUTOR_OIDC_ISSUER", self.executor_oidc_issuer),
            ("GNSIS_EXECUTOR_OIDC_AUDIENCE", self.executor_oidc_audience),
            ("GNSIS_EXECUTOR_TRUSTED_WORKFLOW_SHA", self.executor_trusted_workflow_sha),
        ):
            if not value:
                missing.append(name)
        return missing

    @property
    def execution_configured(self) -> bool:
        return not self.missing_execution_vars()

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

    def missing_production_vars(self, role: Optional[str] = None) -> List[str]:
        """Names of required-for-production settings that are absent, by role.

        Split by service role so each Railway service fails loudly for exactly
        what it needs:

        * ``api`` — HTTP/auth, the model gateway (OpenRouter), webhook signing,
          GitHub App (for internal source-token minting) and executor OIDC/audience.
        * ``worker`` — queue, database, GitHub publishing (App key) and the
          executor dispatch settings. It does NOT need OpenRouter or the webhook
          secret, and does not need browser/engine settings.

        Used by the startup check to fail loudly and actionably rather than
        limping along and rejecting every user request at runtime.
        """
        role = role or self.service_role
        missing: List[str] = []

        if role == "beat":
            # Beat only enqueues periodic recovery/observation tasks. It needs the
            # database and Redis (validated by from_env) plus broker backend; do
            # not require API-only secrets or GitHub/App executor credentials.
            return []

        # API and worker need the GitHub App private key + id: the API mints
        # customer source tokens; the worker dispatches and publishes.
        for name, value in (
            ("GITHUB_APP_ID", self.github_app_id),
            ("GITHUB_APP_PRIVATE_KEY", self.github_app_private_key),
            ("GITHUB_APP_SLUG", self.github_app_slug),
        ):
            if not value:
                missing.append(name)

        # API and worker need the public-beta execution configuration: without it
        # there is no permitted way to run a user job.
        missing.extend(self.missing_execution_vars())

        if role == "api":
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
            # The model gateway runs in the API and needs the upstream key.
            if not self.openrouter_api_key:
                missing.append("OPENROUTER_API_KEY")
            if not self.github_webhook_secret:
                missing.append("GITHUB_WEBHOOK_SECRET")

        # de-duplicate while preserving order
        seen = set()
        ordered = []
        for name in missing:
            if name not in seen:
                seen.add(name)
                ordered.append(name)
        return ordered

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
        allowed_models = [
            m.strip()
            for m in os.environ.get("GNSIS_RUN_ALLOWED_MODELS", "").split(",")
            if m.strip()
        ] or list(DEFAULT_ALLOWED_MODELS)

        def _int(name: str, default: int) -> int:
            raw = os.environ.get(name)
            return int(raw) if raw not in (None, "") else default

        def _float(name: str, default: float) -> float:
            raw = os.environ.get(name)
            return float(raw) if raw not in (None, "") else default

        def _bool(name: str, default: bool = False) -> bool:
            raw = (os.environ.get(name) or "").strip().lower()
            if not raw:
                return default
            return raw in ("1", "true", "yes", "on")

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
            # -- public-beta remote execution --------------------------------
            execution_provider=os.environ.get("GNSIS_EXECUTION_PROVIDER"),
            public_api_url=os.environ.get("GNSIS_PUBLIC_API_URL"),
            executor_owner=os.environ.get("GNSIS_EXECUTOR_OWNER"),
            executor_repo=os.environ.get("GNSIS_EXECUTOR_REPO"),
            executor_workflow=os.environ.get("GNSIS_EXECUTOR_WORKFLOW", "execute.yml"),
            executor_ref=os.environ.get("GNSIS_EXECUTOR_REF", "main"),
            executor_oidc_issuer=os.environ.get(
                "GNSIS_EXECUTOR_OIDC_ISSUER", "https://token.actions.githubusercontent.com"
            ),
            executor_oidc_audience=os.environ.get("GNSIS_EXECUTOR_OIDC_AUDIENCE"),
            executor_trusted_workflow_sha=os.environ.get(
                "GNSIS_EXECUTOR_TRUSTED_WORKFLOW_SHA"
            ),
            executor_token_ttl_seconds=_int("GNSIS_EXECUTOR_TOKEN_TTL_SECONDS", 1800),
            run_token_ttl_seconds=_int("GNSIS_RUN_TOKEN_TTL_SECONDS", 1800),
            executor_timeout_seconds=_int("GNSIS_EXECUTOR_TIMEOUT_SECONDS", 1800),
            executor_source_max_bytes=_int("GNSIS_EXECUTOR_SOURCE_MAX_BYTES", 262_144_000),
            executor_callback_max_bytes=_int("GNSIS_EXECUTOR_CALLBACK_MAX_BYTES", 10_485_760),
            executor_patch_max_bytes=_int("GNSIS_EXECUTOR_PATCH_MAX_BYTES", 5_242_880),
            executor_event_max_bytes=_int("GNSIS_EXECUTOR_EVENT_MAX_BYTES", 5_242_880),
            run_max_model_calls=_int("GNSIS_RUN_MAX_MODEL_CALLS", 50),
            run_max_input_tokens=_int("GNSIS_RUN_MAX_INPUT_TOKENS", 500_000),
            run_max_output_tokens=_int("GNSIS_RUN_MAX_OUTPUT_TOKENS", 100_000),
            run_max_cost_usd=_float("GNSIS_RUN_MAX_COST_USD", 3.00),
            run_allowed_models=allowed_models,
            model_metadata=_parse_json_env("GNSIS_MODEL_METADATA"),
            service_role=os.environ.get("GNSIS_SERVICE_ROLE", "api"),
            litellm_url=os.environ.get("GNSIS_LITELLM_URL"),
            litellm_api_key=os.environ.get("GNSIS_LITELLM_API_KEY"),
            litellm_callback_secret=os.environ.get("GNSIS_LITELLM_CALLBACK_SECRET"),
            markup_rate=os.environ.get("GNSIS_MARKUP_RATE", "0.05"),
            rate_card_version=os.environ.get("GNSIS_RATE_CARD_VERSION", "beta-2026-07"),
            default_currency=os.environ.get("GNSIS_DEFAULT_CURRENCY", "USD"),
            stripe_webhook_secret=os.environ.get("STRIPE_WEBHOOK_SECRET"),
            balance_reserve_estimate_usd=os.environ.get("GNSIS_BALANCE_RESERVE_ESTIMATE_USD", "0.05"),
            beta_credit_max_usd=os.environ.get("GNSIS_BETA_CREDIT_MAX_USD", "50.00"),
            welcome_credit_enabled=_bool("GNSIS_WELCOME_CREDIT_ENABLED", False),
            welcome_credit_usd=os.environ.get("GNSIS_WELCOME_CREDIT_USD", "5.00"),
            welcome_credit_per_run_usd=os.environ.get(
                "GNSIS_WELCOME_CREDIT_PER_RUN_USD", "0.50"
            ),
            welcome_credit_campaign=os.environ.get(
                "GNSIS_WELCOME_CREDIT_CAMPAIGN", "beta-2026-07"
            ),
            platform_daily_provider_limit_usd=os.environ.get(
                "GNSIS_PLATFORM_DAILY_PROVIDER_LIMIT_USD", ""
            ),
            virtual_key_pepper=os.environ.get("GNSIS_VIRTUAL_KEY_PEPPER", ""),
        )


_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """Process-wide settings singleton, loaded lazily from the environment."""
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings
