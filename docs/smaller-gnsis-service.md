# Panoptic visual understanding and decision service

Panoptic is an integrated visual understanding and grounded decision service.
An agent can ask what is currently visible without setting a task, or supply a
goal and ask for the next bounded decision. A trusted host supplies the rolling
live screen stream and remains solely responsible for permissions, confirmation,
execution, and verification.

## Trust boundary

The service:

- consumes a host-owned persistent screen stream;
- keeps bounded temporal visual and action history;
- describes visible text, elements, locations, state, and recent changes;
- selects one action from the task's code-generated legal action set;
- abstains with `wait` when the policy is uncertain;
- returns a decision tied to the frame that produced it;
- preserves idempotency by replaying the result for a repeated `request_id`.

The service does not:

- capture a screen;
- accept a screenshot as part of a decision request;
- inspect DOM, selectors, cookies, or credentials;
- click, type, navigate, or execute arbitrary commands;
- grant action authority to model output or page content.

## API lifecycle

All control-plane calls except `/health` require a bearer token. The host
credential is required to create and close sessions and to record attempts.
`POST /v1/visual/sessions` returns a host-only stream token and a session-scoped
planner token. The planner token can set/reset the task, request decisions, and
request task-independent perception and read state for that session only; it
cannot create/close sessions or record attempts. A planner token used for
another session or a host-only operation returns 403. Invalid credentials
return 401; unknown sessions return 404 to hosts and 401 to planners. The host
token can also perform planner operations.

```text
Authorization: Bearer <host token or session-scoped planner token>
```

1. `POST /v1/visual/sessions`
   creates a bounded rolling visual session and returns the stream and planner
   tokens.
2. The host opens
   `/v1/visual/sessions/{session_id}/stream?token={stream_token}`.
3. For each frame, the host sends one `screen.frame` JSON header followed by one
   JPEG, WebP, or PNG binary message.
4. `POST /v1/visual/sessions/{session_id}/perceptions`
   returns the current visible scene and recent visible changes without requiring
   a task. The response includes a summary, visible text, visible elements with
   pixel boxes and confidence, the current frame ID, and up to four ordered frame
   IDs from the latest one-second rolling visual window.
   An optional `focus` question narrows the description. An optional
   `target: {"x", "y"}` (current-viewport pixel, nothing is drawn on the frame)
   adds a `grounding` object: what is visibly at that point, with `status`
   (`grounded`, `unresolved`, or `failed`), `label`, `text`, a `box` that is
   only present when it contains the point, a validated `confidence`, and the
   `source` model. MiniCPM keeps describing the rolling scene; a lazily loaded
   Florence-2 sidecar grounds the point, with an optional Qwen3-VL fallback
   (`GNSIS_VISUAL_FALLBACK_GROUNDER=qwen`, off by default).
5. `PUT /v1/visual/sessions/{session_id}/task`
   binds the goal and legal action set.
6. `POST /v1/visual/sessions/{session_id}/decisions`
   returns the next decision and a single-use `decision_id`.
7. The host checks that the decision is still current, applies its own authority,
   permission, and confirmation policy, then executes or rejects it.
8. `POST /v1/visual/sessions/{session_id}/attempts`
   records that the decision was attempted. It never performs the action.
9. Reset or close the session when the task ends.

## Running the service

Set the host credential and start the service with the model backbone and JEV
head checkpoints:

```bash
export GNSIS_VISUAL_HOST_TOKEN=<host-secret>
smaller-gnsis-serve \
  --model /path/to/backbone \
  --head /path/to/jev-head.pt \
  --device cuda:0 \
  --dtype bfloat16 \
  --host 127.0.0.1 \
  --port 8790 \
  --max-sessions 32
```

The model stack is loaded only after the required host token is present. The
service listens on loopback by default; use HTTPS termination and a protected
network when exposing it beyond the local machine.
Uvicorn bounds each incoming WebSocket message to 8 MiB plus 4 KiB, matching
the maximum binary frame and text-header sizes.

Frame IDs must be unique inside a session and capture timestamps must increase
monotonically. Frame bytes and decoded pixel counts are bounded. A stale,
duplicate, malformed, or oversized frame is rejected before reaching the policy.
The 128-entry recent duplicate window rejects repeated frame IDs; `frame_seq`
increments on every accepted frame and is the authoritative currentness key.
Both state and decision responses include it so the host can reject decisions
from an older frame generation.

Decision and perception replay retention are each bounded to the most recent
128 request IDs. Repeating a request ID in that window returns the original
result. Once evicted, its request ID is retained in a bounded 4096-entry
expired-ID window and returns `request_expired`; use a new request ID rather
than retrying an expired one. The current frame must remain unchanged while a
perception response is generated, otherwise the request returns
`stale_perception`.

A still screen is not re-inferred. When every frame in the rolling window shows
the same screen as a perception already produced for the same `focus`, `target`
and viewport, the response reuses that perception and names the original request
in `reused_from` (otherwise `null`); its `frame_id` and `observed_frame_ids`
still identify the frames that were actually examined, and `current` is true
because the current frame is visually the same. Only perceptions of a still
window are kept for reuse, at most eight per session, as 48x30 thumbnails.

## Client SDKs

Open-source clients are available in [`sdks/python`](../sdks/python) and
[`sdks/typescript`](../sdks/typescript). They call the API and stream frames
captured by the trusted host; they do not capture screens, grant permissions,
or execute decisions. The host remains responsible for its own authority and
permission policy and for carrying out any permitted action.

The clients retry health, state, session close, task setup/reset, perception,
and decision requests after transport failures or HTTP 502/503/504 responses. Session
creation is not retried to avoid accidentally creating multiple sessions, and
`record_attempt` is not retried because attempts are single-use. MCP remains the
adapter for MCP-capable agents; these SDKs provide direct client integrations
for hosts and applications.

## Existing host connectors

The browser connector uses the existing GNSIS Browser Hub rather than adding a
second actuator. Run `gnsis-visual-browser-host`, then open the extension Hub at
`hub.html?ws=8766`. It keeps one capture session active for the task, forwards
the Hub's live tab frames at four frames per second while decisions and actions
are in progress, requests bounded decisions, routes them through
`BrowserActionBridge`, records
correlated attempts, and starts each action from a fresh frame.

The desktop connector is part of the existing Electron Host. It is enabled only
when all three values are present:

```text
GNSIS_VISUAL_BASE_URL=https://visual.example
GNSIS_VISUAL_HOST_TOKEN=<host credential>
GNSIS_VISUAL_TASK=<explicit user task>
```

It observes only frames already accepted by `HostSession.sendScreenFrame` and
routes `click`, `type`, `navigate`, and `back` decisions through the existing
`ActionBroker` and `ToolRegistry`. The broker still owns capability
negotiation, trusted-turn policy, confirmation, permissions, replay protection,
timeline events, and post-action screen verification. Without a matching
trusted user turn, side-effecting actions fail closed into the broker's existing
confirmation policy.

## Commercial access

Commercial hosts use the existing Genesis virtual-key system. Issue a virtual
key with the `visual:host` scope, exchange it at
`POST /v1/visual/grants`, and send the returned short-lived grant as the
visual API's host bearer credential. Grants bind a workspace, key, project,
environment, and bounded concurrent-session, frame, and decision limits; the
runtime verifies them offline. Sessions from different workspaces cannot access
each other's state. Usage callbacks meter accepted frames, frame bytes,
perceptions, decisions (including act/abstain), recorded attempts, inference
milliseconds, session milliseconds, inspections, pixel reads, history reads,
and closed sessions. Session milliseconds are incremental deltas in periodic
reports and the final close report, not a cumulative duration: sum them once
per unique `event_id`. Time spanning midnight is split across UTC days.
Pricing, charging, and billing are intentionally not implemented.

`GET /v1/visual/usage?days=30` on the control plane returns `totals`, `by_day`,
`by_key`, and `by_client` aggregates for the last 1–90 UTC days. A dashboard
session can read its workspace (optionally `virtual_key_id=...`); a
`visual:host` key can read only its own usage. Session counts are distinct
within each aggregate, so grouped session counts need not sum to the total.

The optional `X-Panoptic-Client` header (falling back to `User-Agent`) records
the host creating the session and the most recent authenticated planner
client. These bounded labels are self-reported diagnostics, not authenticated
agent identity or a billing authority; never send credentials in them.
Usage is attributed per reporting interval, not per individual client call.
Legacy callbacks may omit the new counters and labels.

Metering requires grant and usage-sink configuration on both the control plane
and runtime. Sessions with no accepted frame and no authenticated request for
`GNSIS_VISUAL_IDLE_TIMEOUT_S` (default 120 seconds; `0` disables) are closed by a
background sweep, and their final usage report is sent like a host close. Idle
time before that close is counted; later requests get `unknown_session`. Runtime
queues remain process-local; a crash can lose undelivered reports and time
since the last report. This ledger is not yet a crash-safe billing system.
Grants remain valid until expiry (up to the configured TTL, 300 seconds by
default) after a key is disabled or rotated. The daily decision quota is checked
when issuing grants against ingested usage, so it is a soft limit that can lag
by the usage-sink interval.

## MCP adapter

The local stdio adapter (`panoptic-mcp`) ships in the lightweight Python SDK and
uses the official MCP Python SDK. Install it without the runtime package:

```bash
pip install "gnsis-visual-sdk[mcp] @ git+https://github.com/aubincorinaldiecooper-bit/GNSISBACKEND.git#subdirectory=sdks/python"
```

`smaller-gnsis-mcp` remains as an alias for existing configurations.

| Tool | Hint | Purpose |
| --- | --- | --- |
| `visual_perceive` | read-only | Describe the current screen, optionally focused or grounded at a pixel |
| `visual_state` | read-only | Task, bounded history, current frame and usage state |
| `visual_history` | read-only | Retained frames and earlier perception answers |
| `visual_inspect` | read-only | Enlarged full-resolution crop of a retained frame, returned as an image |
| `visual_read_pixels` | read-only | Exact colours of a retained frame region |
| `visual_set_task` | session write | Set the goal and allowed action set |
| `visual_decide` | session write | One grounded action proposal for the current frame |
| `visual_reset` | session write | Clear the goal and visual action history |

Every tool declares `destructiveHint: false`; only the five reading tools declare
`readOnlyHint: true`. Clients decide how to use these hints for approval.
`visual_decide` only proposes an action; it never executes one.

It intentionally exposes no session-creation, stream-token, screenshot-upload,
attempt-recording, browser-control, or arbitrary-execution tool. The trusted
host creates the session and stream first, then gives the session-scoped planner
token to the adapter:

```text
GNSIS_VISUAL_API_BASE=https://visual.example
GNSIS_VISUAL_API_TOKEN=<planner token from create_session>
GNSIS_VISUAL_SESSION_ID=<host-created session>
GNSIS_VISUAL_CLIENT=claude-code   # optional usage label, default panoptic-mcp/0.1.0
```

The adapter sends `GNSIS_VISUAL_CLIENT` as `X-Panoptic-Client`, so usage
summaries can be grouped by agent. The label is self-reported, not verified
identity. Non-loopback API bases must use HTTPS. Error messages never repeat the
planner token.

Claude, Codex, Hermes, OpenClaw, Big GNSIS, or another MCP-compatible planner
can use the same tool contract. The adapter forwards to the API and contains no
parallel policy or execution implementation. Planner tokens are per session:
update the agent configuration when the host starts a new session.

### Claude Code

```bash
claude mcp add \
  --transport stdio \
  --env GNSIS_VISUAL_API_BASE=https://visual.example \
  --env GNSIS_VISUAL_API_TOKEN=<session-planner-token> \
  --env GNSIS_VISUAL_SESSION_ID=<host-created-session> \
  --env GNSIS_VISUAL_CLIENT=claude-code \
  panoptic \
  -- panoptic-mcp
```

### Codex

In `~/.codex/config.toml`:

```toml
[mcp_servers.panoptic]
command = "panoptic-mcp"
enabled = true

[mcp_servers.panoptic.env]
GNSIS_VISUAL_API_BASE = "https://visual.example"
GNSIS_VISUAL_API_TOKEN = "<session-planner-token>"
GNSIS_VISUAL_SESSION_ID = "<host-created-session>"
GNSIS_VISUAL_CLIENT = "codex"
```

If you restrict tools with `enabled_tools`, keep `visual_perceive`: it is the
main read of the current screen.

### OpenClaw

```bash
openclaw mcp add panoptic \
  --command panoptic-mcp \
  --env GNSIS_VISUAL_API_BASE=https://visual.example \
  --env GNSIS_VISUAL_API_TOKEN=<session-planner-token> \
  --env GNSIS_VISUAL_SESSION_ID=<host-created-session> \
  --env GNSIS_VISUAL_CLIENT=openclaw
openclaw mcp doctor panoptic --probe
```

`openclaw mcp doctor` flags literal sensitive environment values; prefer
OpenClaw's secret mechanisms for the planner token where available.

These examples use a local adapter because the host already owns the live visual
session. A future remote Streamable HTTP MCP endpoint must use OAuth and
protected-resource metadata rather than embedding long-lived credentials.

## Commercialization sequence

1. Stabilize and version the visual session, stream, decision, and attempt
   contracts.
2. Add device-bound or OAuth-backed tenant identity, scoped credentials,
   rotation, revocation, quotas, and durable usage metering.
3. Publish host SDKs that stream frames and enforce local permission,
   confirmation, current-frame, execution, and verification rules.
4. Add persistent replayable traces with explicit privacy and retention policy.
5. Benchmark supported planners on identical goals, frames, action sets, and
   host outcomes.
6. Promote policy versions only through held-out task, security, latency, cost,
   abstention, grounding, and recovery gates.

Laya remains a benchmark baseline until the retirement gate in
`docs/migrations/visual-system1.md` passes.

## Planner evaluation

Claude, Codex, Hermes, OpenClaw, and other planners must be compared through the
same API and MCP contract. Each evaluation cohort freezes:

- Smaller GNSIS policy, backbone, head, and action-contract versions;
- task goal, initial application state, rendered frame stream, and legal actions;
- host permission and confirmation policy;
- decision, retry, and wall-clock budgets;
- network and model-serving conditions.

The planner sees only contract data available in production. Expected actions,
target boxes, hidden page state, DOM, selectors, and final outcomes remain
scoring-only labels.

Every run records:

- valid-action and abstention correctness;
- click grounding and current-frame safety;
- permission or confirmation violations;
- host execution and post-action verification outcomes;
- recovery efficiency, total decisions, latency, and estimated cost;
- verified multi-step task completion.

Run at least 50 multi-step browser and desktop tasks per planner, with repeated
seeds where nondeterminism exists. Report confidence intervals and failure
categories rather than one aggregate success number. A planner integration is
supported only when it produces no invalid or unauthorized executions and meets
the published completion, latency, and cost thresholds.
