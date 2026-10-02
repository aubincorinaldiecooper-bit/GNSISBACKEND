# GNSIS Visual SDK for Python

`gnsis-visual-sdk` calls the Smaller GNSIS visual API and streams frames
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

## GNSIS Browser host

The SDK includes a connector for GNSIS Browser's existing Hub protocol. It
reuses the extension's live tab capture, provenance checks, bounded actuator,
permission prompt, replay protection, and action evidence instead of creating a
second browser execution stack.

```bash
gnsis-visual-browser-host \
  --base-url http://127.0.0.1:8765 \
  --host-token "$GNSIS_VISUAL_HOST_TOKEN" \
  "Open the requested page and complete the visible task"
```

The connector listens on `127.0.0.1:8766` by default. Open the GNSIS Browser
Hub with `?ws=8766`; after the extension approves the connection, the connector:

1. creates one visual session and starts the existing `capture.frame` stream;
2. sends each live tab frame through `FrameStream`;
3. requests one bounded Smaller GNSIS decision;
4. sends it through the existing `browser.action` authority contract;
5. records the correlated action attempt and repeats from a fresh frame.

Capture is stopped before each action and restarted afterward, so a decision
cannot be executed against a frame from a previous visual state.
