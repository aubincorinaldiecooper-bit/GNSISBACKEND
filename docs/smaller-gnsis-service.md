# Smaller GNSIS visual execution service

Smaller GNSIS is a model-independent visual decision service. An agent supplies a
goal and asks for the next bounded decision. A trusted host supplies the live
screen stream and remains solely responsible for permissions, confirmation,
execution, and verification.

## Trust boundary

The service:

- consumes a host-owned persistent screen stream;
- keeps bounded temporal visual and action history;
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

All control-plane calls require:

```text
Authorization: Bearer <service credential>
```

The current bearer credential is a deployment bootstrap boundary, not the final
commercial identity system. Production identity must replace it with scoped,
rotatable credentials and tenant isolation without changing the versioned visual
contract.

1. `POST /v1/visual/sessions`
   creates a bounded decision session and returns a host-only stream token.
2. The host opens
   `/v1/visual/sessions/{session_id}/stream?token={stream_token}`.
3. For each frame, the host sends one `screen.frame` JSON header followed by one
   JPEG, WebP, or PNG binary message.
4. `PUT /v1/visual/sessions/{session_id}/task`
   binds the goal and legal action set.
5. `POST /v1/visual/sessions/{session_id}/decisions`
   returns the next decision and a single-use `decision_id`.
6. The host checks that the decision is still current, applies its own authority,
   permission, and confirmation policy, then executes or rejects it.
7. `POST /v1/visual/sessions/{session_id}/attempts`
   records that the decision was attempted. It never performs the action.
8. Reset or close the session when the task ends.

Frame IDs must be unique inside a session and capture timestamps must increase
monotonically. Frame bytes and decoded pixel counts are bounded. A stale,
duplicate, malformed, or oversized frame is rejected before reaching the policy.

## MCP adapter

The local stdio adapter uses the official open-source MCP Python SDK. It exposes:

- `visual_set_task`
- `visual_decide`
- `visual_record_attempt`
- `visual_state`
- `visual_reset`

It intentionally exposes no session-creation, stream-token, screenshot-upload,
browser-control, or arbitrary-execution tool. The trusted host creates the
session and stream first, then starts the adapter with:

```text
GNSIS_VISUAL_API_BASE=https://visual.example
GNSIS_VISUAL_API_TOKEN=<scoped credential>
GNSIS_VISUAL_SESSION_ID=<host-created session>
```

Run the adapter with:

```bash
smaller-gnsis-mcp
```

Claude, Codex, Hermes, OpenClaw, Big GNSIS, or another MCP-compatible planner
can use the same tool contract. The adapter forwards to the API and contains no
parallel policy or execution implementation.

### Claude Code

Claude Code supports local stdio servers through `claude mcp add`:

```bash
claude mcp add \
  --transport stdio \
  --env GNSIS_VISUAL_API_BASE=https://visual.example \
  --env GNSIS_VISUAL_API_TOKEN=<scoped-credential> \
  --env GNSIS_VISUAL_SESSION_ID=<host-created-session> \
  smaller-gnsis \
  -- smaller-gnsis-mcp
```

### Codex

Codex supports local stdio servers in `~/.codex/config.toml`:

```toml
[mcp_servers.smaller_gnsis]
command = "smaller-gnsis-mcp"
enabled = true
enabled_tools = [
  "visual_set_task",
  "visual_decide",
  "visual_record_attempt",
  "visual_state",
  "visual_reset"
]

[mcp_servers.smaller_gnsis.env]
GNSIS_VISUAL_API_BASE = "https://visual.example"
GNSIS_VISUAL_API_TOKEN = "<scoped-credential>"
GNSIS_VISUAL_SESSION_ID = "<host-created-session>"
```

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
