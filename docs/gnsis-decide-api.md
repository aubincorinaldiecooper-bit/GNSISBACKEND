# GNSIS Decide API (developer beta)

Hosted visual-decision endpoint. Send a request plus offered candidates; get
back a typed verdict — never prose to parse.

**Endpoint**: `https://gnsis.studio/v1/decide` — the site front door proxies
here (GNSISFRONTEND `Caddyfile`, same pattern as `/ws/*`), so a Bearer key is
all an external caller needs.
**Internal service**: `https://aubincorinaldiecooper--gnsis-decide-decide-service.us-east.modal.run`
(Modal `gnsis-decide`, `modal/gnsis_decide.py`). Direct calls to this URL
need the workspace `Modal-Key`/`Modal-Secret` pair — internal only.
**Status**: the front door is wired in GNSISFRONTEND#82; it goes live once
`GNSIS_DECIDE_UPSTREAM` is set on the frontend container and that deploy lands.
**Latency (beta)**: ~2–4s per decide, first call on a cold container ~60–70s.
Faster path (~0.26s measured) is disabled until an upstream GPU-kernel bug is
resolved; the beta serves the verified-stable reference path.

## Auth

Every call needs `Authorization: Bearer <developer key>`. Keys are issued per
developer (the service accepts a list; each key is interchangeable at the API —
the key identifies the caller to us). Missing or wrong key → `401`.

## POST /v1/decide

```json
{
  "requestId": "unique-id-for-your-call",
  "text": "the trusted user request",
  "step": 0,
  "history": [{"step": 0, "result": "what already happened"}],
  "tools": ["fs_open", "browser_open"],
  "tool_schemas": [{"name": "fs_open", "description": "open a file or app"}],
  "state_image": "optional base64 PNG of current screen"
}
```

Response — the verdict (fields only appear where they apply):

```json
{
  "kind": "tool",
  "tool": "fs_open",
  "args": {},
  "confidence": 0.93
}
```

`args` is `{}` when the request text does not map onto the tool schema
unambiguously — the verdict says *which* candidate won; filling executable
arguments is the caller's side of the contract. `confidence` is emitted only
on `kind=tool` verdicts; `done` carries `message`, `fallback` carries
`reason`, and neither reports a confidence.

`kind=fallback` is the honest-abstain answer: the model judged no offered
action safe enough. Treat it as "I don't know" — it exists so agents can stop
instead of guessing.

## GET /healthz

`{"ok": true}` — unauthenticated readiness probe.

## Access note for the beta team

The endpoint sits behind Modal proxy auth in addition to the Bearer key (the
workspace blocks public URLs). Developer-facing access goes through the site's
front door — `https://gnsis.studio/v1/decide` and `/v1/healthz` are
reverse-proxied to this endpoint with `Modal-Key`/`Modal-Secret` stamped
server-side (GNSISFRONTEND `Caddyfile`, same pattern as `/ws/*`), so external
callers only ever need their Bearer key. Direct calls to the Modal URL still
require the workspace proxy pair — keep those internal.

## Cost shape

L4 GPU, scales to zero when idle (5-min window). Each decide ~2–4s of GPU time
≈ $0.0004–0.0009/call at $0.80/hr; idle costs $0.
