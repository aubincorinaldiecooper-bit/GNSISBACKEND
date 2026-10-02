# GNSIS Visual SDK for Python

`gnsis-visual-sdk` calls the Smaller GNSIS visual API and streams frames
captured by a trusted host. It does not capture screens, execute actions, or
grant permissions.

```python
from gnsis_visual_sdk import FrameStream, VisualClient

with VisualClient("https://visual.example", api_token) as client:
    session = client.create_session()
    try:
        async with await FrameStream.connect(
            "https://visual.example", session
        ) as stream:
            await stream.send_frame(
                "frame-1",
                captured_at_ms=host.capture_timestamp_ms(),
                image=host.capture_jpeg(),
            )
            client.set_task(session.session_id, "click the control", ["click"])
            result = client.decide(session.session_id)
            decision = result["decision"]
            if result["current"] and host.permission_policy_allows(decision):
                host.execute(decision)
                client.record_attempt(session.session_id, result["decision_id"])
    finally:
        client.close_session(session.session_id)
```

The host supplies the live frame and remains responsible for current-state
checks, permission policy, confirmation, execution, and verification. The SDK
retries idempotent health/state/task/session-close/decision requests on
transport errors and HTTP 502/503/504. It does not retry session creation or
single-use `record_attempt` requests.
