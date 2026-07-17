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
2. **Create two services from this repo.** Both build from the included
   **`Dockerfile`** (Python + Node + the Claude Code CLI + git — the worker needs
   Node because the Claude Agent SDK drives the Claude Code CLI under the hood).
   Set each service's start command:
   - **web** — `uvicorn gnsis.service.api:app --host 0.0.0.0 --port $PORT`
   - **worker** — `celery -A gnsis.service.tasks.celery_app worker --loglevel=info --concurrency=2`
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
| `GNSIS_CORS_ORIGINS` | `*` | Comma-separated browser origins allowed to call the API. |

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

Open the **web** service URL in a browser — `/` serves a built-in UI (`/ui`) to
create jobs, watch status + logs, review the diff, and approve/reject. Or use the
API directly:

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

## Engines (swappable)

The agent that writes the code is pluggable behind one interface, chosen per job
(`"engine"` in the create-job body) or by `GNSIS_DEFAULT_ENGINE`:

| Engine | What it is | Needs |
| --- | --- | --- |
| `claude` | Anthropic Claude Agent SDK (the Claude Code harness). | `ANTHROPIC_API_KEY` + Node/CLI (in the image). |
| `openhands` | OpenHands — open-source, Python, autonomous. Driven headless; the diff is the result. | OpenHands installed; `GNSIS_OPENHANDS_*`. |
| `mock` | Deterministic, offline — for smoke tests. | nothing |

Running both lets you A/B engines on real work and keep the one that wins.

| Var | Default | Purpose |
| --- | --- | --- |
| `GNSIS_OPENHANDS_MODEL` | `anthropic/claude-opus-4-8` | Model OpenHands uses (`LLM_MODEL`). |
| `GNSIS_OPENHANDS_CMD` | `["python","-m","openhands.core.main","-t","{task}"]` | Headless invocation (JSON list; `{task}`/`{workspace}` substituted). Tune per OpenHands version. |

## Learning loop (gets better over time)

GNSIS learns from **both** human decisions at the gate, and both writes are
approval-gated:

- **Approve → publish:** the change is remembered as an `accepted_change` for the
  repo (a positive example / convention).
- **Reject (with a reason):** the rejection is distilled into a `lesson` —
  *"we tried X, it was rejected because Y; avoid that."*

Before each new job, relevant repo memory (accepted changes + lessons) is injected
into the engine's context, so the agent stops repeating mistakes and follows
learned conventions. This is engine-agnostic — it improves whichever engine runs.

## Long-term memory (specialization)

GNSIS keeps a **repo-scoped, approval-gated** long-term memory in Postgres
(`GNSIS_MEMORY=postgres`, the default). Before each job, relevant memory for that
repo is injected into the engine's context; after a change is **approved and
published**, a record of it is written back. Only validated outcomes are
remembered, so the memory stays high-signal — and over time the agent specializes
to your codebase's conventions and decisions. Set `GNSIS_MEMORY=none` to disable.

| Var | Default | Purpose |
| --- | --- | --- |
| `GNSIS_MEMORY` | `postgres` | `postgres` (durable, repo-scoped) or `none`. |

## Sandboxing (executing model-written code)

The worker runs the engine's edits and the project's tests — untrusted code.

- **`GNSIS_SANDBOX=none`** (default): runs in the worker's own container. On
  Railway, that container is ephemeral and isolated per deploy, which is
  acceptable for dogfooding **your own** repos.
- **`GNSIS_SANDBOX=docker`**: runs each job's engine in an ephemeral,
  resource-limited, non-root container that can only see the job's workspace
  (`Dockerfile.sandbox`). Phase events are streamed back so Postgres
  checkpointing is unaffected.

> **Railway caveat:** Docker-in-Docker is **not** available on Railway's standard
> runtime, so `docker` mode requires running the **worker** on a Docker-capable
> host (a VM, Fly.io Machines, etc.). On Railway, stay on `none` and rely on the
> ephemeral worker container.

Build the sandbox image (on a Docker host):

```bash
docker build -f Dockerfile.sandbox -t gnsis-sandbox:latest .
```

| Var | Default | Purpose |
| --- | --- | --- |
| `GNSIS_SANDBOX` | `none` | `none` or `docker`. |
| `GNSIS_SANDBOX_IMAGE` | `gnsis-sandbox:latest` | Image to run per job. |
| `GNSIS_SANDBOX_NETWORK` | `bridge` | Container network (needs model-API egress). |
| `GNSIS_SANDBOX_MEMORY` | `2g` | Memory limit. |
| `GNSIS_SANDBOX_CPUS` | `2` | CPU limit. |
| `GNSIS_SANDBOX_TIMEOUT` | `1800` | Max seconds per sandboxed run. |

## A note on safety

Even with `GNSIS_SANDBOX=docker`, the container needs network egress to reach the
model API, so isolation is filesystem/resource/privilege-based, not network-air-
gapped. For dogfooding your **own** repos either mode is fine; do not point GNSIS
at untrusted repositories until egress is also locked down (e.g. an allowlist
proxy).
