# Public-beta remote execution (GitHub Actions)

Every user coding job runs **only** in the fixed workflow in the private executor
repo (`aubincorinaldiecooper-bit/Gnsis-studio-`). No model-written code or
customer command runs in the Railway API or the Celery worker; there is no local,
Celery-process, Daytona or DockerEngine fallback.

## Architecture

```
user submits task
 → backend authenticates user, resolves workspace/repo, resolves immutable base SHA
 → worker dispatches execute.yml (job_id + dispatch_nonce ONLY); persists workflow run id
 → GitHub provisions a fresh ubuntu-24.04 VM
 → VM authenticates to GNSIS via OIDC (audience https://api.gnsis.studio)
 → single-use nonce consumed → short-lived, hashed run token issued
 → VM pulls spec + immutable source (exact base SHA, single-use, size-capped)
 → hardened container runs the agent; egress only via a default-deny proxy;
   models only via the restricted gateway (budget-enforced)
 → container returns only patch.diff/tests.json/receipt.json/events.jsonl
 → trusted host + backend validate outputs against the clean base; hash the patch
 → job → awaiting_approval
 → user approves exact base SHA + exact patch SHA-256
 → worker mints a fresh customer token, reconstructs the base, applies the exact
   patch, opens a DRAFT PR (never pushes to main, never auto-merges)
 → the customer repo's own CI independently verifies the PR
```

## Services and secrets by role

| Variable | API | Worker |
|----------|-----|--------|
| `DATABASE_URL`, `REDIS_URL` | ✅ | ✅ |
| `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`, `GITHUB_APP_SLUG` | ✅ (source token minting) | ✅ (dispatch + publish) |
| `OPENROUTER_API_KEY` | ✅ (model gateway) | ❌ |
| `GITHUB_WEBHOOK_SECRET` | ✅ | ❌ |
| Better Auth (`BETTER_AUTH_*`, `GNSIS_AUTH_INTERNAL_*`) | ✅ | ❌ |
| `GNSIS_EXECUTION_PROVIDER` + `GNSIS_EXECUTOR_*` + budgets | ✅ | ✅ |

Set `GNSIS_SERVICE_ROLE=api` on the web service and `GNSIS_SERVICE_ROLE=worker`
on the worker so startup validation checks the right requirements. In production
(Postgres `DATABASE_URL`) a missing required variable **fails startup**.

## Migration

The schema is applied by the existing idempotent migration
(`create_all` + additive columns):

```bash
gnsis-migrate         # or: python -c "from gnsis.service.db import init_db; init_db()"
```

This adds `execution_runs`, `execution_model_calls`, `execution_events`.

## Trusting the executor commit

`GNSIS_EXECUTOR_TRUSTED_WORKFLOW_SHA` must be the exact commit SHA of the
executor's `main`. Dispatch refuses to run if `main`'s head has drifted from it,
and OIDC verification requires the token's workflow SHA to equal it. After any
change to the executor `main`, re-audit and update this variable.

## Smoke test

1. `GET /health` → `{"status":"ok", ...}`.
2. Create a job (`POST /jobs`) for a repo your GitHub App is installed on.
3. Confirm a run appears in the executor repo's Actions tab (`gnsis-run <job_id>`).
4. Watch the job reach `awaiting_approval` with a diff (`GET /jobs/{id}/diff`).
5. Confirm no customer code ran in Railway (worker logs show only dispatch/reconcile).
6. Approve; confirm a **draft** PR opens on the customer repo and CI runs there.
7. Cancel a fresh job mid-run; confirm the run token is revoked and the workflow
   run is cancelled.
