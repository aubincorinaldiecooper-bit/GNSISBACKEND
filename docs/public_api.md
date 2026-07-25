# The GNSIS public API (`/v1`)

GNSIS is usable by external coding agents, IDEs, CLIs and CI systems without the
web composer. The web application is a reference client over this same API.

The product loop this API exists to serve:

```
create a run  ->  GNSIS pins repository, model, policy and approved intelligence
              ->  the coding agent executes
              ->  GNSIS records evidence, usage and an immutable receipt
              ->  a human or authorized system approves
              ->  approved outcomes become durable repository intelligence
              ->  a later run (even on a different model) consumes it
              ->  that run's receipt records exactly what influenced it
```

Internally a run is still a `job`; the public contract is run-oriented and
stable. Both names refer to the same object.

---

## Authentication

Send a Genesis virtual key as a bearer token:

```
Authorization: Bearer gns_live_xxxxxxxxxxxxxxxxxxxxxxxx
```

Keys are minted per workspace (`POST /v1/virtual-keys`); the secret is returned
exactly once and only a peppered SHA-256 is stored. A key is bound to one
workspace and can additionally be restricted to specific models.

The dashboard's session JWT is also accepted on these routes, which is how the
reference web client uses them. **Do not rely on the session as the
authentication method for a machine client** — issue a key.

### Scopes

| Scope | Grants |
|---|---|
| `repositories:read` | list/read repositories and branches |
| `runs:create` | create runs and follow-ups |
| `runs:read` | read runs and lifecycle events |
| `runs:approve` | approve or reject a run |
| `receipts:read` | read receipts |
| `intelligence:read` | read and query repository intelligence |

A key issued before scopes existed (`api_scopes` NULL) carries the full set
above. Such keys are already workspace-bound, so this widens nothing across a
tenant boundary.

Missing scope → `403 authorization_failed`.

---

## Errors

Every public error uses one envelope. No stack traces, no secrets.

```json
{
  "error": {
    "code": "repository_access_denied",
    "message": "The API key cannot access this repository.",
    "request_id": "req_9987bbd586524f1691669b90"
  }
}
```

Stable codes: `authentication_failed`, `authorization_failed`,
`repository_access_denied`, `invalid_model`, `insufficient_balance`,
`spending_limit_exceeded`, `idempotency_conflict`, `executor_unavailable`,
`dispatch_failed`, `run_not_found`, `invalid_run_state`, `receipt_unavailable`,
`intelligence_unavailable`, `invalid_request`.

## Pagination

List endpoints take `limit` and `offset` and return:

```json
{ "object": "list", "data": [], "has_more": false, "total": 0, "limit": 20, "offset": 0 }
```

## Idempotency

`POST /v1/runs` and `POST /v1/runs/{run_id}/follow-ups` accept an
`Idempotency-Key` header. A repeat with the same workspace + key returns the
original run and never creates or bills a second one. Enforced by a partial
unique index on `(workspace_id, idempotency_key)`, so concurrent replays collide
in the database rather than racing through.

---

## End-to-end example

### 1. List repositories

```bash
curl -s https://api.gnsis.studio/v1/repositories \
  -H "Authorization: Bearer $GNSIS_API_KEY"
```

### 2. Create Run A (model A)

```bash
curl -s -X POST https://api.gnsis.studio/v1/runs \
  -H "Authorization: Bearer $GNSIS_API_KEY" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: run-a-2026-07-25-001" \
  -d '{
        "repository_id": "repo_abc123",
        "instruction": "Harden the authentication middleware",
        "base_branch": "main",
        "model": "anthropic/claude-opus-4.8"
      }'
```

```json
{
  "id": "job_bbec96036ff8",
  "object": "run",
  "repository_id": "repo_abc123",
  "repository": "octo/alpha",
  "branch": "main",
  "instruction": "Harden the authentication middleware",
  "model": "anthropic/claude-opus-4.8",
  "advisor_model": null,
  "status": "queued",
  "thread_id": "job_bbec96036ff8",
  "parent_run_id": null,
  "created_at": "2026-07-25T16:04:06+00:00"
}
```

The server — never the client — resolves the GitHub installation, the base
commit, the policy version, the allowed models and the approved intelligence.
Clients cannot supply installation tokens, provider credentials, executor
credentials, a workspace identity, or a policy override.

### 3. Read lifecycle events

```bash
curl -s https://api.gnsis.studio/v1/runs/job_bbec96036ff8/events \
  -H "Authorization: Bearer $GNSIS_API_KEY"
```

```json
{
  "object": "list",
  "data": [
    {"id": "evt_job_bbec96036ff8_0", "run_id": "job_bbec96036ff8", "sequence": 0,
     "type": "run.created", "at": "2026-07-25T16:04:06+00:00", "payload": {"has_instruction": true}},
    {"id": "evt_job_bbec96036ff8_1", "run_id": "job_bbec96036ff8", "sequence": 1,
     "type": "run.queued", "at": "2026-07-25T16:04:06+00:00", "payload": {}},
    {"id": "evt_job_bbec96036ff8_2", "run_id": "job_bbec96036ff8", "sequence": 2,
     "type": "run.dispatch_started", "at": "...", "payload": {"execution_run_id": "exec_..."}},
    {"id": "evt_job_bbec96036ff8_3", "run_id": "job_bbec96036ff8", "sequence": 3,
     "type": "executor.installation_lookup_started", "at": "...", "payload": {"repository_id": "repo_abc123"}}
  ],
  "has_more": false, "total": 4, "limit": 100, "offset": 0
}
```

Event types include `run.created`, `run.queued`, `run.dispatch_started`,
`executor.installation_lookup_started`, `executor.workflow_dispatched`,
`run.execution_started`, `tool.called`, `tests.completed`,
`run.awaiting_approval`, `run.completed`, `run.failed`, `run.blocked`,
`run.approved`, `run.rejected`, `receipt.ready`, `policy.pinned`,
`intelligence.consumed`.

**A failure before the executor starts still produces events.** A run blocked in
preflight (for example, a repository with no initial commit) reports
`run.dispatch_started`, `executor.installation_lookup_started` and `run.blocked`
— never an empty timeline.

### 4. Read the receipt

```bash
curl -s https://api.gnsis.studio/v1/runs/job_bbec96036ff8/receipt \
  -H "Authorization: Bearer $GNSIS_API_KEY"
```

Every terminal attempt has a receipt, including one that failed before
execution. Known-zero values are reported as zero — never "not tracked yet":

```json
{
  "object": "receipt",
  "run_id": "job_bbec96036ff8",
  "execution_run_id": "exec_aaac252807b4",
  "repository": "octo/alpha",
  "model": "anthropic/claude-opus-4.8",
  "advisor_model": null,
  "status": "blocked",
  "execution_started": false,
  "model_calls": 0,
  "tokens": {"input": 0, "output": 0, "cached": 0, "reasoning": 0},
  "cost": {"provider_cost": "0", "gnsis_service_fee": "0", "total_billed": "0", "currency": "USD"},
  "files_changed": [],
  "tests": "not_run",
  "failure_category": "blocked_repository_empty",
  "failure_message": "GNSIS couldn't start this run because …",
  "memory_ids_consumed": [],
  "policy": {"name": "genesis", "version": 1, "hash": "…"}
}
```

Receipts are assembled from immutable records, so a later pricing, policy or
model change never rewrites a historical receipt.

### 5. Approve Run A

```bash
curl -s -X POST https://api.gnsis.studio/v1/runs/job_bbec96036ff8/approve \
  -H "Authorization: Bearer $GNSIS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"note": "looks right"}'
```

Approval is idempotent and preserves actor identity, timestamp and note.

**Approval is the trust boundary for reusable intelligence.** Publication of the
pull request remains a separate step that follows approval; intelligence is
captured at approval so a client never has to wait on the publish to succeed.
Rejected runs never yield active accepted-change intelligence.

### 6. Read the intelligence it produced

```bash
curl -s https://api.gnsis.studio/v1/repositories/repo_abc123/intelligence \
  -H "Authorization: Bearer $GNSIS_API_KEY"
```

```json
{
  "object": "list",
  "data": [
    {
      "id": "mem_5f2c1d",
      "object": "intelligence",
      "repository_id": "repo_abc123",
      "content": "Harden the authentication middleware",
      "type": "accepted_change",
      "status": "active",
      "source_run_id": "job_bbec96036ff8",
      "created_at": "2026-07-25T16:10:00+00:00"
    }
  ],
  "has_more": false, "total": 1, "limit": 20, "offset": 0
}
```

Each item traces back to the run that produced it, the approval that authorized
it, and that run's evidence. Content is a bounded, durable lesson — entire logs,
patches and receipts are deliberately **not** stored as intelligence; the item
references that evidence instead.

Preview what a task would retrieve, using the same deterministic ranker that
selects context at dispatch:

```bash
curl -s -X POST https://api.gnsis.studio/v1/repositories/repo_abc123/intelligence/query \
  -H "Authorization: Bearer $GNSIS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"task": "Modify authentication middleware", "limit": 10}'
```

Repository and workspace scope are enforced server-side regardless of the query.

### 7. Create Run B on a different model

```bash
curl -s -X POST https://api.gnsis.studio/v1/runs \
  -H "Authorization: Bearer $GNSIS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
        "repository_id": "repo_abc123",
        "instruction": "Add rate limiting to the authentication middleware",
        "model": "openai/gpt-5.4"
      }'
```

### 8. Confirm Run B consumed Run A's intelligence

```bash
curl -s https://api.gnsis.studio/v1/runs/$RUN_B/receipt \
  -H "Authorization: Bearer $GNSIS_API_KEY"
```

```json
{
  "run_id": "job_77ab91",
  "model": "openai/gpt-5.4",
  "memory_ids_consumed": ["mem_5f2c1d"],
  "policy": {"name": "genesis", "version": 1, "hash": "…"}
}
```

`mem_5f2c1d` was **produced** under `anthropic/claude-opus-4.8` and **consumed**
under `openai/gpt-5.4` — the intelligence outlives the model that created it.

The selected intelligence is delivered to the executor as a **separate field**,
never spliced into the user's instruction, and the exact ids are pinned to the
run, so a historical receipt still identifies what was consumed even after that
intelligence is later superseded or disabled.

---

## Follow-up runs

```bash
curl -s -X POST https://api.gnsis.studio/v1/runs/$RUN_A/follow-ups \
  -H "Authorization: Bearer $GNSIS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"instruction": "now add tests for that change"}'
```

The client supplies only the new instruction. Workspace, repository, branch,
thread identity, primary model, Advisor and policy are resolved authoritatively
from the parent.

A follow-up is a **new immutable run** — new run id, same `thread_id`, parent in
`parent_run_id`, with its own events, receipt, cost, approval boundary and
intelligence-consumption record. The parent is never mutated, resumed, or
appended to. Cross-workspace parent references are rejected. A legacy run with
no thread metadata becomes the root of its thread on first follow-up.

---

## Run statuses

| Status | Meaning |
|---|---|
| `queued` | accepted, awaiting dispatch |
| `planning` / `patching` / `testing` / `summarizing` | the agent is executing |
| `awaiting_approval` | a change is proposed and needs a decision |
| `approved` / `publishing` | approved; the pull request is being opened |
| `completed` | settled successfully |
| `rejected` | reviewed and declined |
| `failed` | execution or infrastructure began and then failed |
| `blocked` | a prerequisite was missing, so execution never began |
| `cancelled` | cancelled before completion |

`blocked` is deliberately distinct from `failed`: nothing executed, so no usage
was consumed and the receipt reports true zeros. The specific reason is in
`failure_category` (`blocked_repository_empty`, `blocked_branch_not_found`,
`blocked_installation_inaccessible`).

---

## Backward compatibility

The pre-existing `/jobs*` routes remain available and unchanged for the current
web client. `/v1/repositories`, `/v1/repositories/{id}/branches` and `/v1/models`
already existed and are unchanged — they now additionally accept an API key.
