# Configurable spending limits (G5)

Opt-in, tunable, concurrency-safe spending controls evaluated on every gateway
request. Limits are **never globally disabled** — each policy chooses its
enforcement mode. Balance enforcement (G4) and these policy limits both run before
the request is forwarded.

## Policy (`limit_policies`)

| Field | Meaning |
|---|---|
| `scope_type` | `workspace` / `project` / `environment` / `user` / `team` / `virtual_key` |
| `scope_id` | the id of that scope |
| `limit_type` | `per_run` / `daily` / `monthly` / `total` (the window) |
| `amount` | the cap (decimal string) |
| `enforcement_mode` | `observe_only` / `warn` / `block` |
| `warning_threshold` | fraction 0–1; warn at this % of the cap |
| `reset_period`, `effective_at`, `expires_at` | window + validity |

A virtual key's own inline limits (`hard`/`soft`/`per_run`/`daily`/`monthly`) are
evaluated as synthetic `virtual_key`-scoped policies (hard/per-run/daily/monthly =
block, soft = warn).

## Evaluation — deterministic, most-restrictive, auditable

For each request the engine collects **every** applicable policy across the
request's scopes, and for each computes `projected = committed_spend(window) +
in-flight_holds + estimated_request_cost`:

- `block` + projected > cap → **deny** (`spending_limit_exceeded`). If several
  block policies apply, the tightest wins (most restrictive).
- `warn` (exceeded, or ≥ warning threshold) → **allow** + warning.
- `observe_only` → **allow**, decision recorded only.

Every evaluation writes a `limit_decisions` row (policy, scope, threshold,
previous usage, reserved amount, mode, result) — an immutable audit trail. Actual
usage is filled in at reconcile.

## Concurrency safety

Evaluation runs **under the per-workspace lock** (the billing anchor), and the
enforced policies place **one reservation per distinct (scope, window)** for the
request's estimated exposure (`limit_reservations`) — deduplicated, so a key's
soft *and* hard limit (both `virtual_key`/`total`) share a single hold rather than
double-counting, while a workspace daily *and* monthly cap hold against their two
separate windows. So several concurrent requests cannot all consume the same
remaining allowance — the second sees the first's hold. When
the real charge lands, `reconcile` releases the hold (the charge now counts in
committed spend); a failed request `release`s it. This is the same
reserve-then-reconcile pattern as the balance hold — never a read-then-write race.

## API (workspace-scoped)

`POST /v1/limits`, `GET /v1/limits`, `PATCH /v1/limits/{id}` (amount / mode /
enabled / expiry), `GET /v1/balances`.

## Gateway wiring

Between the balance check and forwarding, the gateway calls the engine; a `block`
result returns `402 spending_limit_exceeded` with `details.scope` +
`details.limit_id`. On success the limit holds reconcile alongside the charge; on
provider failure they release with the balance hold.

## Migration

`gnsis-migrate` adds `limit_policies`, `limit_decisions`, `limit_reservations`
(new tables; additive, idempotent).
