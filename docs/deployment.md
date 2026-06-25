# Running GNSIS on Railway

GNSIS runs as **two long-lived services** plus **two managed datastores**:

| Component | What it is | Why |
| --- | --- | --- |
| **web** | FastAPI HTTP API (`gnsis.service.api:app`) | Create jobs, read status/logs/diff, approve/reject. Thin: it only reads Postgres and enqueues work. |
| **worker** | Celery worker (`gnsis.service.tasks.celery_app`) | Runs the long generation pipeline and, after approval, opens the PR. |
| **Postgres** | Railway Postgres plugin | The durable store — jobs, logs, checkpoints, diffs, approvals, PR metadata, prompt versions. Nothing is lost on restart/teardown. |
| **Redis** | Railway Redis plugin | Celery broker + result backend. |

The HTTP API **never** runs an evolution/generation loop inside a request — it
enqueues a Celery task and returns immediately. The worker does the long work,
checkpoints each phase to Postgres, and **stops at `awaiting_approval`**. Only
after a human approves does the separate `publish_pr` task mint a scoped GitHub
App token, push a branch, and open the PR.

## Architecture

```
client ──HTTP──▶ FastAPI (web)
                   │  POST /jobs           → enqueue run_job ─┐
                   │  GET  /jobs/{id}       (read Postgres)   │
                   │  GET  /jobs/{id}/logs  (read Postgres)   │
                   │  POST /jobs/{id}/approve → enqueue ──────┼─┐
                   ▼                                          │ │
                 Postgres ◀── checkpoints/logs/diff ──────────┘ │
                   ▲                                            │
                 Redis ◀── queue ──▶ Celery (worker) ◀──────────┘
                                       │ run_job: clone → plan → patch
                                       │          → tests → summary
                                       │          → awaiting_approval
                                       └ publish_pr: GitHub App token →
                                                    push branch → open PR
```

## Setup on Railway

1. **Create a project** and add the **Postgres** and **Redis** plugins.
2. **Create two services from this repo:**
   - **web** — start command:
     `uvicorn gnsis.service.api:app --host 0.0.0.0 --port $PORT`
   - **worker** — start command:
     `celery -A gnsis.service.tasks.celery_app worker --loglevel=info --concurrency=2`

   Both install with `pip install -e ".[service]"` (see `railway.json` /
   `Procfile`).
3. **Set the environment variables** (below) on **both** services.
4. Deploy. The schema is created automatically (the API on startup, the worker on
   boot); you can also run `gnsis-migrate` as a one-off.

## Environment variables

Set these on **both** the web and worker services.

**Required**

| Var | Value |
| --- | --- |
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` |
| `REDIS_URL` | `${{Redis.REDIS_URL}}` |
| `ANTHROPIC_API_KEY` | Your Anthropic key — the Claude Agent SDK talks to Anthropic directly (not OpenRouter). |

**Required for opening PRs** (worker)

| Var | Value |
| --- | --- |
| `GITHUB_APP_ID` | Your GitHub App's ID. |
| `GITHUB_APP_INSTALLATION_ID` | The installation ID on the target repo/org. |
| `GITHUB_APP_PRIVATE_KEY` | The App's PEM private key (multiline; Railway supports multiline values). |

**Optional**

| Var | Default | Purpose |
| --- | --- | --- |
| `GNSIS_DEFAULT_ENGINE` | `claude` | Engine to use when a job doesn't specify one. |
| `GNSIS_DEFAULT_BASE_BRANCH` | `main` | Base branch for new jobs. |
| `GNSIS_WORKSPACE_ROOT` | `/tmp/gnsis-workspaces` | Where repos are cloned per job. |
| `GNSIS_WORKER_CONCURRENCY` | `2` | Celery worker concurrency. |
| `GNSIS_API_KEY` | _unset_ | If set, the API requires `Authorization: Bearer <key>`. |
| `GNSIS_ALLOWED_REPOS` | _unset_ | Comma-separated allowlist; empty = any repo. |

### The GitHub App

Create a GitHub App (Settings → Developer settings → GitHub Apps) with
**Repository permissions**: *Contents: Read & write* and *Pull requests: Read &
write*. Install it on the repos GNSIS may open PRs against, then copy the App ID,
the installation ID, and a generated private key into the variables above. GNSIS
mints a short-lived installation token per publish — tokens are never persisted.

## Lifecycle

```
queued → planning → patching → testing → summarizing → awaiting_approval
   → approved → publishing → completed
   → rejected
   → failed
```

## Using it

```bash
# create a job
curl -X POST "$BASE/jobs" -H 'content-type: application/json' \
  -d '{"repo":"owner/name","instruction":"Add a /health endpoint"}'

# watch it
curl "$BASE/jobs/<id>"
curl "$BASE/jobs/<id>/logs"
curl "$BASE/jobs/<id>/diff"

# approve → opens the PR
curl -X POST "$BASE/jobs/<id>/approve" -d '{"actor":"you"}'
```

## A note on safety

The worker executes model-written changes (and runs tests) inside its container.
That is acceptable for dogfooding your **own** repos on Railway's ephemeral
workers. Do not point GNSIS at untrusted repositories until stronger sandboxing
is in place.
