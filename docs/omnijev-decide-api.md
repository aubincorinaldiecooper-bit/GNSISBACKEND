# OmniJev Decide API (developer beta)

Hosted visual-decision endpoint. Send a request plus offered candidates; get
back a typed verdict — never prose to parse.

**Endpoint**: `https://aubincorinaldiecooper--omnijev-decide-decide-service.us-east.modal.run`
**App**: `omnijev-decide` (Modal, `modal/omnijev_decide.py`)
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

Response — the verdict:

```json
{
  "kind": "tool | done | fallback",
  "tool": "name when kind=tool",
  "args": {},
  "message": "final answer when kind=done",
  "reason": "why it abstained when kind=fallback",
  "confidence": 0.93
}
```

`kind=fallback` is the honest-abstain answer: the model judged no offered
action safe enough. Treat it as "I don't know" — it exists so agents can stop
instead of guessing.

## GET /healthz

`{"ok": true}` — unauthenticated readiness probe.

## Access note for the beta team

The endpoint currently sits behind Modal proxy auth in addition to the Bearer
key (the workspace blocks public URLs). For developer-facing access either:

1. allow unauthenticated URLs on the `main` environment (workspace setting), or
2. front this path through the site's existing proxy (same pattern as `/ws/*`).

Until one is in place, direct external calls need the workspace's
`Modal-Key`/`Modal-Secret` pair — keep those internal.

## Cost shape

L4 GPU, scales to zero when idle (5-min window). Each decide ~2–4s of GPU time
≈ $0.0004–0.0009/call at $0.80/hr; idle costs $0.
