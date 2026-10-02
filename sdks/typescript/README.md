# Smaller GNSIS Visual SDK for TypeScript

`@gnsis/visual-sdk` is a client for the Smaller GNSIS visual API. The trusted
host remains responsible for screen capture, permission checks, and action
execution; this SDK only streams frames and exchanges task/decision data.

```ts
import { FrameStream, VisualClient } from "@gnsis/visual-sdk";

const client = new VisualClient({
  baseUrl: "http://127.0.0.1:8765",
  apiToken: process.env.GNSIS_VISUAL_HOST_TOKEN!,
});
const session = await client.createSession();
const planner = new VisualClient({
  baseUrl: "http://127.0.0.1:8765",
  apiToken: session.plannerToken,
});
const stream = await FrameStream.connect(
  "http://127.0.0.1:8765",
  session,
);

try {
  const jpeg = await trustedHost.captureJpeg();
  await stream.sendFrame({
    frameId: crypto.randomUUID(),
    capturedAtMs: Date.now(),
    image: jpeg,
  });
  await planner.setTask(session.sessionId, "Click the requested control", [
    "click",
  ]);
  const result = await planner.decide(session.sessionId);
  const decision = result.decision as { action: string; [key: string]: unknown };

  if (trustedHost.permissionPolicyAllows(decision)) {
    await trustedHost.execute(decision);
    await client.recordAttempt(session.sessionId, String(result.decision_id));
  }
} finally {
  await stream.close();
  await client.closeSession(session.sessionId);
}
```

Only health, state, session close, task setup/reset, and decisions are retried
after transport failures or HTTP 502/503/504 responses. Session creation and
attempt recording are not retried because their outcomes may be non-idempotent.
`createSession`, `closeSession`, and `recordAttempt` require the host token.
The host receives a session-scoped `plannerToken` for planner task, decision,
and state operations.
