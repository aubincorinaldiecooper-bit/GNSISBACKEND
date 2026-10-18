---
name: panoptic
description: Give an agent persistent vision through the Panoptic API — open a session, stream frames from a trusted host, ask grounded questions about the screen, and get one grounded next-action proposal. Use when building agents that need to see a desktop, browser tab, or app window.
---

# Panoptic

Panoptic is a perception service for agents. A **trusted host** (a desktop app, a
browser driver, a robot) owns the screen and every action on it; Panoptic only
looks. It keeps a bounded history of frames and answers questions or proposes a
single next action, grounded at exact pixels.

**Panoptic never acts.** It cannot click, type, scroll or capture anything on its
own. Every action it proposes must be carried out by your host.

## Access model

1. Exchange your workspace API key for a short-lived grant:
   `POST https://api.gnsis.studio/v1/visual/grants` with
   `Authorization: Bearer $GNSIS_API_KEY`.
2. The host opens a session with the grant:
   `POST $PANOPTIC_API_BASE/v1/visual/sessions`. The response returns the session
   id and **two tokens** — so there are **three credentials** in play:
   - **the grant** — authenticates all host HTTP operations (set task via the
     host path, `record_attempt`, close). The grant expires after ~5 minutes:
     keep the workspace API key in the trusted host and mint a fresh grant when
     one is needed; closing a session still accepts an expired grant.
   - **`stream.token`** — authenticates the session's WebSocket only. It is
     independent of grant expiry, so streaming can keep a session alive after
     the grant lapses.
   - **`planner.token`** — read and plan only inside that one session. Hand this
     to the agent; it cannot open, close or stream to anything.
3. Send `X-Panoptic-Client: <your-agent>/<version>` with requests so usage is
   attributed to your agent.

Sessions end when the host closes them, or after **2 minutes** with no frames
and no requests — keep streaming or the session is released.

## Fastest path: panoptic-mcp

If the agent is MCP-aware (Claude Code, Codex, OpenClaw, …), it gets the session
as tools with zero client code. The host opens the session, then the agent runs:

```bash
pip install "gnsis-visual-sdk[mcp] @ git+https://github.com/aubincorinaldiecooper-bit/GNSISBACKEND.git#subdirectory=sdks/python"
```

MCP config (stdio server, session-scoped):

```json
{
  "mcpServers": {
    "panoptic": {
      "command": "panoptic-mcp",
      "env": {
        "GNSIS_VISUAL_API_BASE": "https://<your-panoptic-endpoint>",
        "GNSIS_VISUAL_API_TOKEN": "<session planner token>",
        "GNSIS_VISUAL_SESSION_ID": "<session id>"
      }
    }
  }
}
```

Tools it exposes (all inside that one session):

| Tool | What it does |
| --- | --- |
| `visual_perceive` | Describe the current screen, optionally focused on a question or grounded at a pixel |
| `visual_state` | Task, bounded history, current frame and usage state |
| `visual_history` | Retained frames and earlier perception answers |
| `visual_inspect` | Enlarged full-resolution crop of a retained frame |
| `visual_read_pixels` | Exact colours of a retained frame region |
| `visual_set_task` | Set the goal and allowed action set |
| `visual_decide` | One grounded action proposal for the current frame; never executes |
| `visual_reset` | Clear the goal and visual action history |

## Python SDK

```python
from gnsis_visual_sdk import VisualClient

with VisualClient(base_url, api_token) as client:
    session = client.create_session()         # session_id, stream_path, planner_token
    client.set_task(session.session_id, "Save the document", allowed_actions=["click", "type"])
    # … host streams frames to session.stream_path …
    decision = client.decide(session.session_id)        # one grounded action proposal
    # host decides whether to execute; record only what it actually did:
    if host_executed:
        client.record_attempt(session.session_id, decision["decision_id"])   # grant (host credential) only
    answer = client.perceive(session.session_id, focus="Did the save dialog close?")
    client.close_session(session.session_id)
```

`VisualClient` methods: `create_session`, `close_session`, `set_task`,
`reset_task`, `decide`, `perceive`, `record_attempt`, `state`, `history`,
`inspect`, `read_pixels`. TypeScript: `npm install @gnsis/visual-sdk`.
`record_attempt` requires the grant — only the trusted host reports what
it actually executed.

## The decide loop

A grounded agent loop looks like:

1. **Set the task** — `set_task` with the goal and the allowed action set, so
   proposals stay inside what the host can actually do.
2. **Stream** — the host pushes frames to the session's WebSocket. This is all
   Panoptic ever sees.
3. **Decide or perceive** — `decide` returns one proposal nested under
   `decision` (`{"request_id", "decision_id", "decision": {"action", "target":
   {"x","y"}, "reason"}, "gate", "frame_seq", "current"}`); `perceive`
   answers a free-form question about the current screen or a pixel. If frames
   changed while the decision was generated the call answers 409
   `stale_decision` — decide again on the fresh frame.
4. **Host executes** — the host decides whether to carry out the proposal.
5. **Record the attempt** — only when the host actually executed:
   `record_attempt(session_id, decision_id)` moves the proposal into the
   session's action history. A refused proposal must stay unrecorded, or later
   decisions reason from an action that never happened.

Use `inspect`/`read_pixels` when a region needs a closer look before acting, and
`history` to see what changed between two states.

## Boundaries to respect

- Proposals are proposals. The host decides; never claim an action happened
  because `decide` returned it.
- A planner token sees only its own session — design for one watched surface
  per session.
- Answers are grounded at retained frames, not a live feed — re-perceive after
  the host acts instead of assuming state.

## Reference

Full API reference (traditional docs layout): https://gnsis.studio/docs
Endpoint surface: `runtime/gnsis_runtime/gnsis_runtime/visual/api.py` in this repo.
