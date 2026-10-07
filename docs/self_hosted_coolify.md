# Self-hosted control plane (Coolify)

This is the migration target for the GNSIS control plane. Railway remains the
rollback/reference environment until the self-hosted stack has passed the
verification gates below.

The GPU runtime does **not** move in this migration. Modal remains the provider
for GNSIS Live / Panoptic compute, and the worker remains the only control-plane
service that owns Modal workspace credentials. GitHub Actions remains the
customer coding-job executor.

## Production service map

The current Railway production environment is reproduced as these self-hosted
services:

| Service | Source | Port / command |
| --- | --- | --- |
| API | `GNSISBACKEND` | `uvicorn gnsis.service.api:app --host 0.0.0.0 --port 8000` |
| Worker | `GNSISBACKEND` | `celery -A gnsis.service.tasks.celery_app worker --loglevel=info --concurrency=${GNSIS_WORKER_CONCURRENCY:-2}` |
| Beat | `GNSISBACKEND` | `celery -A gnsis.service.tasks.celery_app beat --loglevel=info --pidfile=/tmp/celerybeat.pid --schedule=/tmp/celerybeat-schedule` |
| Postgres | `postgres:18` | private only, persistent volume |
| Redis | `redis:8.2.9` | private only, persistent volume, password required |

Run exactly one Beat replica.

Public routing remains stable:

- `https://api.gnsis.studio` -> API
- `https://auth.gnsis.studio` -> the Better Auth service in GNSISFRONTEND
- `https://gnsis.studio` -> the Caddy/React frontend in GNSISFRONTEND

Do not cut DNS until all verification gates pass against temporary hostnames.

## API environment

Required production names:

```
GNSIS_SERVICE_ROLE=api
DATABASE_URL
REDIS_URL
GITHUB_APP_ID
GITHUB_APP_PRIVATE_KEY
GITHUB_APP_SLUG
GITHUB_WEBHOOK_SECRET
OPENROUTER_API_KEY
BETTER_AUTH_JWKS_URL=https://auth.gnsis.studio/api/auth/jwks
BETTER_AUTH_ISSUER=https://auth.gnsis.studio
BETTER_AUTH_AUDIENCE=gnsis-api
GNSIS_AUTH_INTERNAL_URL=https://auth.gnsis.studio
GNSIS_AUTH_INTERNAL_SECRET
GNSIS_FRONTEND_URL=https://gnsis.studio
GNSIS_EXECUTION_PROVIDER=github_actions
GNSIS_PUBLIC_API_URL=https://api.gnsis.studio
GNSIS_EXECUTOR_OWNER
GNSIS_EXECUTOR_REPO
GNSIS_EXECUTOR_WORKFLOW
GNSIS_EXECUTOR_REF
GNSIS_EXECUTOR_OIDC_ISSUER
GNSIS_EXECUTOR_OIDC_AUDIENCE
GNSIS_EXECUTOR_TRUSTED_WORKFLOW_SHA
GNSIS_API_KEY
GNSIS_VIRTUAL_KEY_PEPPER
```

Preserve the existing run-budget/rate-card variables from production as
applicable. Do not copy Railway-generated `RAILWAY_*` variables.

## Worker environment

The worker receives only worker-required secrets. In particular, do not copy
API-only OpenRouter, Better Auth internal, webhook, or virtual-key secrets merely
because the old Railway service accumulated them.

```
GNSIS_SERVICE_ROLE=worker
DATABASE_URL
REDIS_URL
GITHUB_APP_ID
GITHUB_APP_PRIVATE_KEY
GITHUB_APP_SLUG
GNSIS_EXECUTION_PROVIDER=github_actions
GNSIS_PUBLIC_API_URL=https://api.gnsis.studio
GNSIS_EXECUTOR_OWNER
GNSIS_EXECUTOR_REPO
GNSIS_EXECUTOR_WORKFLOW
GNSIS_EXECUTOR_REF
GNSIS_EXECUTOR_OIDC_ISSUER
GNSIS_EXECUTOR_OIDC_AUDIENCE
GNSIS_EXECUTOR_TRUSTED_WORKFLOW_SHA
MODAL_TOKEN_ID
MODAL_TOKEN_SECRET
MODAL_PROXY_KEY
MODAL_PROXY_SECRET
MODAL_ENVIRONMENT=main
GNSIS_EDGE_SECRET
```

Keep the current GNSIS Modal app/function/model-volume/secret-name overrides only
when they differ from code defaults.

## Beat environment

Beat is private and receives only scheduling dependencies:

```
GNSIS_SERVICE_ROLE=beat
DATABASE_URL
REDIS_URL
CELERY_BROKER_URL
CELERY_RESULT_BACKEND
```

When broker/result variables are omitted, they may default to `REDIS_URL` as
supported by the application. Beat must not receive API, auth, model-gateway, or
Modal secrets.

## Data migration

Postgres is durable state and must be copied before DNS cutover.

1. Take a consistent dump of the Railway Postgres 18 database.
2. Restore it into the self-hosted Postgres 18 instance.
3. Run `gnsis-migrate` against the restored database.
4. Validate row counts for users/auth-linked data, jobs, usage/billing records,
   memory/code-intelligence tables, and deployment metadata.
5. Freeze writes briefly for the final delta/cutover if the source has changed
   after the initial dump.

Redis is the Celery broker/result backend, not the durable application source of
truth. Start the self-hosted Redis clean unless an explicit durable dependency
is discovered before cutover.

## Verification gates

Before public DNS changes, all of the following must pass:

1. API `/health` is healthy on a temporary hostname.
2. Auth `/health` is healthy and its JWKS endpoint is reachable.
3. Frontend `/health` is healthy and `/env.js` points at the temporary API/auth
   endpoints used for pre-cutover testing.
4. Google sign-in completes end to end.
5. Passwordless email via Resend completes end to end.
6. An authenticated frontend request reaches the API.
7. A queued job is consumed by the worker and persists checkpoints to Postgres.
8. Exactly one Beat scheduler is running.
9. Worker Modal status/deploy integration can reach the existing Modal workspace.
10. Frontend websocket proxy reaches the existing GNSIS Live runtime with the
    existing `MODAL_PROXY_KEY`, `MODAL_PROXY_SECRET`, and
    `GNSIS_EDGE_SECRET`.
11. Postgres data survives a service restart.
12. A backup is created and a restore test succeeds.

## Cutover

After the gates pass:

1. Put the old control plane into a short write freeze.
2. Take/restore the final Postgres dump or delta.
3. Change `api.gnsis.studio`, `auth.gnsis.studio`, and `gnsis.studio` to the
   self-hosted ingress.
4. Verify TLS, auth callbacks, API/JWKS, frontend, worker, Beat and live sockets
   from the public hostnames.
5. Keep Railway intact but idle during the rollback window.
6. Only after the rollback window, remove Railway-specific services/config in a
   separate cleanup change.

## Non-goals for this migration

- Moving Modal GPU workloads onto the VPS.
- Changing the GNSIS realtime architecture.
- Replacing GitHub Actions execution isolation.
- Combining API, worker and Beat into one process.
- Rewriting auth.
