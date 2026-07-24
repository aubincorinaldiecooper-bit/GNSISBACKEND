# GNSIS backend

The backend for GNSIS. It is **two things in one package (`gnsis`)**:

1. **The GNSIS service** — a FastAPI + Celery application (the deployed product).
   It authenticates users, orchestrates coding runs on a **hardened, sandboxed
   executor**, meters and bills model usage, issues API keys, and serves an
   OpenAI-compatible model gateway. This is what runs on Railway.
2. **The self-evolution runtime** — the original, dependency-free `gnsis` CLI
   (RSPL/SEPL: versioned prompts, a tool-calling agent, and a
   propose→assess→commit self-evolution loop). Still present and runnable
   offline; it is the lineage this repo grew from, not the deployed service.

> New here? Read **[`ARCHITECTURE.md`](./ARCHITECTURE.md)** first — it maps the
> two layers, the end-to-end run lifecycle, and the security boundaries.

## What the service does

- **Runs.** A run is created over the API, executed asynchronously by a Celery
  worker, checkpointed to Postgres phase by phase, and **paused at
  `awaiting_approval`**. Only after a human approves does a separate task mint a
  scoped GitHub App token and open the pull request. Customer code never runs in
  this process — it runs in the separate **executor** repo (`Gnsis-studio-`), a
  network-firewalled Docker sandbox this service dispatches via GitHub Actions
  and authenticates with GitHub OIDC.
- **Model gateway.** A public, OpenAI-compatible `POST /v1/chat/completions`
  fronted by LiteLLM, authorized with Genesis-native `gns_…` virtual keys, with
  per-call metering.
- **Billing & limits.** Usage ledger, versioned model pricing, pay-as-you-go
  balances, configurable spending limits, Stripe integration, and operator
  credit grants.
- **Repository memory.** Approved, tenant/repository-scoped "intelligence" that
  is injected into future runs as reference-only context.

## Architecture (short version)

```
Frontend ──HTTP──▶ FastAPI (web)  ── enqueue ──▶ Redis ──▶ Celery (worker)
                     │  reads/writes Postgres            │  run pipeline →
                     │                                   │  dispatch executor
                     ▼                                   ▼  (GitHub Actions +
                   Postgres  ◀── checkpoints ────────────┘   OIDC), gateway,
                                                             callbacks, publish
```

Full detail, module map, and trust boundaries are in
[`ARCHITECTURE.md`](./ARCHITECTURE.md). Operational/topic docs live in
[`docs/`](./docs) (deployment, billing, public gateway, spending limits,
virtual keys, usage-ledger integrity, model pricing, LiteLLM metering,
public-beta execution).

## Main technologies

Python 3.9+ · FastAPI · Celery · SQLAlchemy · Postgres · Redis · Pydantic ·
PyJWT + cryptography · LiteLLM (gateway) · Stripe · GitHub App + GitHub OIDC.
The self-evolution **core** is standard-library only.

## Folder structure

```
src/gnsis/
  service/            The FastAPI + Celery service (the deployed product)
    api.py            HTTP app: routers + endpoint handlers
    tasks.py          Celery app + task definitions (run pipeline, publish)
    executor/         Dispatch a run to the sandboxed executor and validate it
                      back: dispatch, oidc, gateway, callbacks, source,
                      validation, publish, reconcile, github, installation, store
    billing.py, usage.py, pricing.py, limits.py   Metering, ledger, pay-as-you-go
    virtual_keys.py, public_gateway.py            gns_ keys + OpenAI-compatible gateway
    codememory.py, intelligence_lifecycle.py      Repository memory
    github_app.py, auth.py, settings.py, orm.py, repository.py, db.py
  orchestration/, evolution/, engines/, memory/, models/, tools/, cli.py
                      The self-evolution runtime (the original `gnsis` CLI)
configs/, examples/   Standalone runtime demo scripts (not imported by the service)
docs/                 Operational + topic documentation
tests/                pytest suite
Dockerfile            Service image (Railway).  Dockerfile.sandbox — local sandbox image.
Procfile              Railway process types: web / worker / beat / release
```

## Local setup

```bash
# Service (needs Postgres + Redis):
pip install -e ".[service]"
export DATABASE_URL=postgresql://user:pass@localhost:5432/gnsis
export CELERY_BROKER_URL=redis://localhost:6379/0
gnsis-migrate                                   # create tables
uvicorn gnsis.service.api:app --reload          # http://localhost:8000
celery -A gnsis.service.tasks.celery_app worker --loglevel=info   # separate terminal

# Self-evolution runtime only (no service, no deps, no API key):
pip install -e .
gnsis demo
```

## Environment variables

`src/gnsis/service/settings.py` is the source of truth; `docs/deployment.md`
lists the full deployment set. The essentials:

| Group | Variables |
|---|---|
| **Datastores** | `DATABASE_URL`, `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND` |
| **Auth** | `BETTER_AUTH_AUDIENCE`, `BETTER_AUTH_ISSUER`, `BETTER_AUTH_JWKS_URL`, `GNSIS_API_KEY` (internal), `GNSIS_AUTH_INTERNAL_SECRET`, `GNSIS_AUTH_INTERNAL_URL` |
| **GitHub App** | `GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID`, `GITHUB_APP_PRIVATE_KEY`, `GITHUB_APP_SLUG`, `GITHUB_WEBHOOK_SECRET` |
| **Executor** | `GNSIS_EXECUTOR_OWNER`, `GNSIS_EXECUTOR_REPO`, `GNSIS_EXECUTOR_WORKFLOW`, `GNSIS_EXECUTOR_REF`, `GNSIS_EXECUTOR_TRUSTED_WORKFLOW_SHA`, `GNSIS_EXECUTOR_OIDC_ISSUER`/`_AUDIENCE`, and the `_MAX_BYTES`/`_TIMEOUT`/`_TOKEN_TTL` limits |
| **Models / gateway** | `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`, `GNSIS_LITELLM_URL`, `GNSIS_LITELLM_API_KEY`, `GNSIS_LITELLM_CALLBACK_SECRET` |
| **Web / CORS** | `GNSIS_FRONTEND_URL`, `GNSIS_CORS_ORIGINS` |
| **Billing / limits** | Stripe keys plus `GNSIS_DEFAULT_CURRENCY`, `GNSIS_BETA_CREDIT_MAX_USD`, `GNSIS_BALANCE_RESERVE_ESTIMATE_USD`, … |

The service is designed so the API process holds no model-provider or GitHub
**write** credentials in the request path — those are used only by the worker /
executor. Never expose any secret to the browser.

## Commands

| Command | What it does |
|---|---|
| `gnsis-migrate` | Create/upgrade the database schema (Railway `release` step). |
| `uvicorn gnsis.service.api:app` | Run the HTTP API (`web`). |
| `celery -A gnsis.service.tasks.celery_app worker` | Run the async worker. |
| `celery -A gnsis.service.tasks.celery_app beat` | Scheduled tasks (reconcile, etc.). |
| `gnsis demo` / `gnsis run` / `gnsis evolve` | The self-evolution runtime CLI. |
| `pytest -q` | Run the test suite (`pip install -e ".[dev]"` first). |

## Deployment

Railway, from the included `Dockerfile` (see `railway.json` + `Procfile`): a
**web** service, a **worker** service, a **beat** scheduler, a **release**
migration step, plus **Postgres** and **Redis** plugins. Full walkthrough:
[`docs/deployment.md`](./docs/deployment.md).

## External services & integrations

Postgres · Redis · OpenRouter / LiteLLM (models + metering) · Stripe (billing) ·
GitHub App + GitHub OIDC (repo access + executor trust) · the **executor** repo
`Gnsis-studio-` (sandboxed runs) · the **auth service** (Better Auth, in the
frontend repo) whose JWTs this backend verifies.

## Known limitations / unfinished areas

- **Dual identity.** The service and the self-evolution runtime coexist in one
  package. That is intentional today, but a new owner should decide whether the
  runtime (`orchestration/`, `evolution/`, `engines/`, `configs/`, `examples/`)
  stays first-class or is spun out — it is woven through ~40 modules, so any
  change there is a deliberate project decision, not a casual cleanup.
- **Large modules.** `service/api.py` (~1k lines), `orm.py`, and `repository.py`
  would each read better split by concern (routers / models / queries).
- **No formatter/linter/type-checker** is configured (tests only).
