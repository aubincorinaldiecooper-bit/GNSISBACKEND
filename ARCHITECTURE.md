# GNSIS backend architecture

This repository is one Python package (`gnsis`) with **two layers**. Knowing
which layer a module belongs to is the fastest way to orient yourself.

| Layer | Lives in | What it is | Deployed? |
|---|---|---|---|
| **Service** | `src/gnsis/service/**` | FastAPI API + Celery worker: runs, executor orchestration, gateway, billing, keys, memory. | **Yes** (Railway). |
| **Runtime** | `src/gnsis/{orchestration,evolution,engines,memory,models,tools}`, `cli.py` | The original dependency-free `gnsis` self-evolution CLI (RSPL/SEPL). | No — CLI/offline. |

Everything below describes the **service** unless noted. The runtime layer is
summarized at the end.

## Processes (Procfile)

- **web** — `uvicorn gnsis.service.api:app`. Thin: authenticates, reads/writes
  Postgres, enqueues work, returns. It never runs a generation loop in-request.
- **worker** — `celery … worker`. Runs the long pipeline and the post-approval
  publish.
- **beat** — `celery … beat`. Periodic tasks (e.g. reconciliation).
- **release** — `gnsis-migrate`. Applies the schema before a new version serves.

Datastores: **Postgres** (durable store) and **Redis** (Celery broker/result).

## End-to-end run lifecycle

```
1. Frontend → POST (create run)         [service/api.py, authenticated JWT]
2. Reserve balance, persist the job     [billing.py, executor/store.py, orm.py]
3. Enqueue Celery task                  [tasks.py]  ──▶ Redis
4. Worker dispatches the executor       [service/executor/dispatch.py]
      → triggers the Gnsis-studio- GitHub Actions workflow (pinned trusted SHA)
5. Executor proves identity via OIDC     [executor/oidc.py]  → short-lived run token
6. Executor pulls spec + immutable source[executor/source.py, api.py]
7. Model calls flow back through the gateway, metered
                                         [executor/gateway.py, public_gateway.py, usage.py]
8. Executor returns exactly four artifacts (patch/tests/receipt/events);
   the host validates them                [executor/validation.py, callbacks.py]
9. Run parks at `awaiting_approval`; receipt persisted
10. Human approves → publish task mints a scoped GitHub App token,
    pushes a branch, opens the PR         [executor/publish.py, github_app.py]
11. Reconciliation settles usage/billing  [executor/reconcile.py, billing.py]
```

Customer code is **never** executed in this backend. It runs only inside the
separate, network-firewalled Docker sandbox in the `Gnsis-studio-` executor
repo, which this service dispatches and trusts by pinned workflow SHA + OIDC.

## Module responsibilities (service)

- **`api.py`** — the FastAPI app and endpoint handlers (`/v1/me`, virtual keys,
  usage, pricing, limits, balances, admin credits) plus mounted routers
  (`executor`, `usage`, `stripe`, `public_gateway`).
- **`tasks.py`** — the Celery app and tasks (run pipeline, publish, reconcile).
- **`executor/`** — one concern per file: `dispatch` (trigger the workflow),
  `oidc` (issue/verify the run token), `gateway` (model proxy for the run),
  `source` (immutable source hand-off), `validation` (four-artifact contract),
  `callbacks` (executor → backend), `publish` (open the PR), `reconcile`
  (settle), `github`/`installation` (App tokens), `store`/`models` (persistence).
- **Billing** — `usage.py` (ledger), `pricing.py` (versioned model prices),
  `limits.py` (spending limits), `billing.py` (pay-as-you-go + Stripe).
- **Gateway / keys** — `public_gateway.py` (OpenAI-compatible endpoint),
  `virtual_keys.py` (`gns_` keys: hashed, scoped).
- **Memory** — `codememory.py` + `intelligence_lifecycle.py` (approved,
  tenant/repo-scoped reference context injected into runs).
- **Platform** — `auth.py` (JWT/JWKS verification + internal key),
  `github_app.py`, `settings.py` (config), `orm.py`/`repository.py`/`db.py`
  (SQLAlchemy models, queries, migrations).

## Data model (Postgres, via `orm.py`)

Jobs / execution runs (state machine + checkpoints, logs, diff, receipt),
usage ledger records, versioned model pricing, spending limits, virtual keys
(hash + scopes), code-memory items, and — from the runtime lineage — versioned
prompt resources.

## Security & trust boundaries

- The **API process** holds no model-provider key and no GitHub **write**
  credential in the request path; those are used only by the worker/executor.
- **Executor trust**: dispatch pins the executor by owner/repo/workflow and a
  **trusted workflow SHA**; the run authenticates back via **GitHub OIDC** with
  a custom audience. See `GNSIS_EXECUTOR_TRUSTED_WORKFLOW_SHA`.
- **User auth**: Better Auth JWTs verified against the auth service's JWKS
  (`BETTER_AUTH_*`). Admin/internal endpoints require `GNSIS_API_KEY` /
  `GNSIS_AUTH_INTERNAL_SECRET`.
- **Virtual keys** are stored only as hashes; the plaintext `gns_…` is shown
  once at creation.
- **Repository memory** is reference-only and never overrides verified policy.

## External integrations

Postgres · Redis · OpenRouter + LiteLLM (models + per-call metering) · Stripe
(billing) · GitHub App + GitHub OIDC · the `Gnsis-studio-` executor · the
Better Auth service (frontend repo).

## The self-evolution runtime (original layer)

`cli.py` + `orchestration/`, `evolution/` (SEPL: `propose → assess → commit`),
`engines/` (tool-calling agents: native / claude / openhands), `memory/`,
`models/` (OpenRouter + offline mock), `tools/`. It versions prompts with
content hashing + lineage + rollback (RSPL) and runs a deterministic offline
demo (`gnsis demo`). It shares no request-path code with the service and is not
part of the Railway deployment. See the README's CLI section.
