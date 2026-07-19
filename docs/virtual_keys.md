# Genesis-native virtual keys (G2)

Scoped inference credentials **issued and validated by Genesis** — the credential
an OpenAI-compatible client presents to the Genesis gateway. Format:

```
gns_live_<random>      gns_test_<random>
```

Genesis generates the secret with a CSPRNG, returns it **exactly once**, and
stores only a SHA-256 (optionally peppered) `key_hash` + a non-secret `key_prefix`
(e.g. `gns_live_ab12cd…`) for display and logging. **The full secret is never
stored and cannot be retrieved after creation.** The full key is never logged.

> Supersedes the LiteLLM-issued keys prototyped in backend PR #14: under the
> gateway-first direction Genesis owns key issuance + validation. Do not merge #14
> alongside this — this is the canonical virtual-key system.

## What a key carries

- **Attribution scopes:** workspace (required), project, environment, user, team.
- **Restrictions:** `allowed_providers`, `allowed_models` (empty = unrestricted).
- **Spend limits (inputs to the limits engine, a later PR):** `soft_limit`,
  `hard_limit`, `per_run_limit`, `daily_limit`, `monthly_limit` (decimal strings).
- **Lifecycle:** `status` (`active`/`disabled`/`rotated`), `expires_at`,
  `rotated_to`, `metadata`, `last_used_at`.

## Validation (what the gateway will call)

`VirtualKeyStore.authenticate(settings, presented_secret)` hashes the presented
key and looks it up. It returns the key view or **`None`** — and `None` is returned
identically for unknown / disabled / rotated / expired keys, so an unauthenticated
caller can't distinguish failure modes. `last_used_at` is stamped on success.

## Lifecycle — disable / rotate over delete

Keys are never hard-deleted (historical usage must stay attributable):

- **Disable** → `status=disabled`; stops authenticating immediately.
- **Rotate** → issues a successor with the **same scopes** (new secret, shown
  once), marks the old key `rotated` with `rotated_to` pointing at the successor.

## API (workspace-scoped; user session auth)

| Method / path | Behaviour |
|---|---|
| `POST /v1/virtual-keys` | Issue a key. Returns `{key: <secret, once>, virtual_key, warning}`. |
| `GET /v1/virtual-keys` | List this workspace's keys (no secrets, no hashes). |
| `GET /v1/virtual-keys/{id}` | One key (workspace-checked; `404` otherwise). |
| `POST /v1/virtual-keys/{id}/disable` | Disable. Idempotent; `404` cross-workspace. |
| `POST /v1/virtual-keys/{id}/rotate` | Retire + issue successor (secret shown once). |

Knowing an id is never sufficient — every read/mutation verifies workspace
ownership.

## Configuration

| Var | Default | Purpose |
|---|---|---|
| `GNSIS_VIRTUAL_KEY_PEPPER` | `""` | Optional server-side pepper mixed into the key hash (defence-in-depth if `key_hash` leaks). **Rotating it invalidates all existing keys.** |

## Migration

`gnsis-migrate` adds the `virtual_keys` table (new table; additive, idempotent).

## Next in the program

The public `POST /v1/chat/completions` gateway (G4) calls `authenticate` to
resolve the key, then attributes/limits/meters the request. The per-key limits
stored here are enforced by the concurrency-safe limits engine (G5).
