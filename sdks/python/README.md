# GNSIS Visual SDK for Python

`gnsis-visual-sdk` calls the Panoptic visual API and streams frames
captured by a trusted host. It does not capture screens, execute actions, or
grant permissions.

```python
import asyncio

from gnsis_visual_sdk import FrameStream, VisualClient

with VisualClient("https://visual.example", host_token) as host:
    session = host.create_session()
    try:
        with VisualClient("https://visual.example", session.planner_token) as planner:

            async def plan_and_act():
                async with await FrameStream.connect(
                    "https://visual.example", session
                ) as stream:
                    await stream.send_frame(
                        "frame-1",
                        captured_at_ms=host.capture_timestamp_ms(),
                        image=host.capture_jpeg(),
                    )
                planner.set_task(session.session_id, "click the control", ["click"])
                result = planner.decide(session.session_id)
                decision = result["decision"]
                if result["current"] and host.permission_policy_allows(decision):
                    host.execute(decision)
                    host.record_attempt(session.session_id, result["decision_id"])

            asyncio.run(plan_and_act())
    finally:
        host.close_session(session.session_id)
```

The host supplies the live frame and remains responsible for current-state
checks, permission policy, confirmation, execution, and verification. The SDK
retries idempotent health/state/task/session-close/decision requests on
transport errors and HTTP 502/503/504. It does not retry session creation or
single-use `record_attempt` requests. `create_session`, `close_session`, and
`record_attempt` require the host token. The host receives a session-scoped
`planner_token` at creation for planner task, decision, and state access.

Each request sends `X-Panoptic-Client` (default `gnsis-visual-sdk-python/0.1.0`)
so usage can be grouped by agent. Pass `client_name="my-agent/1.0"` to label
your integration; the label is self-reported, not verified identity.

## MCP server for agents

The optional `mcp` extra installs `panoptic-mcp`, a stdio MCP server that gives
Claude Code, Codex, OpenClaw or any MCP client read access to a host-owned
session through its planner token. It exposes no capture, click or execution
tools.

```bash
pip install "gnsis-visual-sdk[mcp] @ git+https://github.com/aubincorinaldiecooper-bit/GNSISBACKEND.git#subdirectory=sdks/python"
```

See `docs/smaller-gnsis-service.md` for the tool list and agent configuration.
