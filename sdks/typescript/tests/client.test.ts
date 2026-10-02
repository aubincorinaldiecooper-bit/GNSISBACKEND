import assert from "node:assert/strict";
import {
  execFileSync,
  spawn,
  type ChildProcessWithoutNullStreams,
} from "node:child_process";
import { once } from "node:events";
import { setTimeout as sleep } from "node:timers/promises";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { join, resolve } from "node:path";
import {
  FrameStream,
  VisualClient,
  VisualServiceError,
} from "../src/index.js";

const repoRoot = resolve(fileURLToPath(new URL("../../../", import.meta.url)));
const fixturePath = join(repoRoot, "sdks/testing/fixture_server.py");
const runtimePath = join(repoRoot, "runtime/gnsis_runtime");

async function startFixture(): Promise<{
  baseUrl: string;
  process: ChildProcessWithoutNullStreams;
}> {
  const child = spawn("python", [fixturePath], {
    cwd: repoRoot,
    env: { ...process.env, PYTHONPATH: runtimePath },
    stdio: ["ignore", "pipe", "pipe"],
  });
  const stdout: string[] = [];
  const stderr: string[] = [];
  child.stdout.setEncoding("utf8");
  child.stderr.setEncoding("utf8");
  child.stdout.on("data", (chunk: string) => stdout.push(chunk));
  child.stderr.on("data", (chunk: string) => stderr.push(chunk));
  const deadline = Date.now() + 15_000;

  while (Date.now() < deadline) {
    const ready = stdout.join("").match(/^READY (\d+)$/m);
    if (ready) {
      return { baseUrl: `http://127.0.0.1:${ready[1]}`, process: child };
    }
    if (child.exitCode !== null) {
      throw new Error(
        `fixture server exited before ready: ${stdout.join("")}${stderr.join("")}`,
      );
    }
    await sleep(25);
  }
  child.kill();
  throw new Error(
    `fixture server did not become ready: ${stdout.join("")}${stderr.join("")}`,
  );
}

async function stopFixture(child: ChildProcessWithoutNullStreams): Promise<void> {
  if (child.exitCode !== null || child.signalCode !== null) return;
  const stopped = once(child, "exit");
  child.kill();
  await stopped;
}

async function createJpeg(): Promise<Uint8Array> {
  const base64 = execFileSync(
    "python",
    [
      "-c",
      "import base64, io; from PIL import Image; image = Image.new('RGB', (16, 12), 'red'); output = io.BytesIO(); image.save(output, format='JPEG'); print(base64.b64encode(output.getvalue()).decode())",
    ],
    { cwd: repoRoot, encoding: "utf8" },
  ).trim();
  return new Uint8Array(Buffer.from(base64, "base64"));
}

test("visual SDK completes the real API lifecycle and hides credentials", async (t) => {
  const fixture = await startFixture();
  t.after(() => stopFixture(fixture.process));

  const client = new VisualClient({
    baseUrl: fixture.baseUrl,
    apiToken: "sdk-test-host-token",
  });
  const unauthorized = new VisualClient({
    baseUrl: fixture.baseUrl,
    apiToken: "bad-api-token",
  });
  assert.ok(await client.health());
  await assert.rejects(unauthorized.state("missing"), (error: unknown) => {
    assert.ok(error instanceof VisualServiceError);
    assert.equal(error.code, "unauthorized");
    assert.equal(error.status, 401);
    assert.equal(error.retryable, false);
    assert.equal(JSON.stringify(error).includes("bad-api-token"), false);
    assert.equal(String(error).includes("bad-api-token"), false);
    return true;
  });

  const session = await client.createSession();
  const plannerClient = new VisualClient({
    baseUrl: fixture.baseUrl,
    apiToken: session.plannerToken,
  });
  assert.equal(JSON.stringify(client), "{}");
  assert.equal(client.toString(), "VisualClient");
  assert.equal(String(session).includes(session.streamToken), false);
  assert.equal(String(session).includes(session.plannerToken), false);
  const stream = await FrameStream.connect(fixture.baseUrl, session);
  try {
    const image = await createJpeg();
    const accepted = await stream.sendFrame({
      frameId: "sdk-frame-1",
      capturedAtMs: 1_000,
      image,
    });
    assert.equal(accepted.type, "screen.frame.accepted");
    assert.equal(accepted.frame_seq, 1);
    await assert.rejects(
      stream.sendFrame({
        frameId: "sdk-frame-1",
        capturedAtMs: 1_001,
        image,
      }),
      (error: unknown) => {
        assert.ok(error instanceof VisualServiceError);
        assert.equal(error.code, "replayed_frame");
        return true;
      },
    );
  } finally {
    await stream.close();
  }

  await plannerClient.setTask(session.sessionId, "click the control", ["click"]);
  const first = await plannerClient.decide(session.sessionId, "sdk-request-1");
  const repeated = await plannerClient.decide(session.sessionId, "sdk-request-1");
  assert.equal(first.decision_id, repeated.decision_id);
  const decision = first.decision as { action: string; frame_id: string };
  assert.equal(decision.action, "click");
  assert.equal(decision.frame_id, "sdk-frame-1");

  await client.recordAttempt(session.sessionId, String(first.decision_id));
  await assert.rejects(
    client.recordAttempt(session.sessionId, String(first.decision_id)),
    (error: unknown) => {
      assert.ok(error instanceof VisualServiceError);
      assert.equal(error.code, "unknown_decision");
      return true;
    },
  );
  const state = await plannerClient.state(session.sessionId);
  assert.ok(JSON.stringify(state.history).includes("click"));
  await plannerClient.resetTask(session.sessionId);
  await client.closeSession(session.sessionId);
  await assert.rejects(
    plannerClient.state(session.sessionId),
    (error: unknown) =>
      error instanceof VisualServiceError && error.code === "unauthorized",
  );
});

test("HTTPS or loopback HTTP is required for clients and frame streams", async () => {
  for (const baseUrl of [
    "http://visual.example",
    "http://192.168.1.2:8790",
    "ftp://visual.example",
    "",
    "visual.example",
  ]) {
    assert.throws(
      () => new VisualClient({ baseUrl, apiToken: "secret" }),
      (error: unknown) =>
        error instanceof VisualServiceError &&
        error.code === "insecure_transport",
    );
    await assert.rejects(
      FrameStream.connect(baseUrl, {
        sessionId: "session-1",
        streamPath: "/stream",
        streamToken: "stream-secret",
        plannerToken: "planner-secret",
        protocol: "screen-frame-v1",
      }),
      (error: unknown) =>
        error instanceof VisualServiceError &&
        error.code === "insecure_transport",
    );
  }

  for (const baseUrl of [
    "http://localhost:8790",
    "http://127.0.0.1:8790",
    "http://[::1]:8790",
  ]) {
    new VisualClient({ baseUrl, apiToken: "secret" });
  }
});

test("frame streams connect over loopback HTTP", async () => {
  const originalDescriptor = Object.getOwnPropertyDescriptor(
    globalThis,
    "WebSocket",
  );
  const urls: string[] = [];
  class FakeWebSocket extends EventTarget {
    static readonly OPEN = 1;
    readyState = 0;

    constructor(readonly url: string | URL) {
      super();
      urls.push(String(url));
      queueMicrotask(() => {
        this.readyState = FakeWebSocket.OPEN;
        this.dispatchEvent(new Event("open"));
      });
    }

    send(_data: string | ArrayBufferLike | Blob | ArrayBufferView): void {}

    close(): void {
      this.readyState = 3;
    }
  }
  Object.defineProperty(globalThis, "WebSocket", {
    configurable: true,
    value: FakeWebSocket as unknown as typeof WebSocket,
  });
  try {
    for (const baseUrl of [
      "http://localhost:8790",
      "http://127.0.0.1:8790",
      "http://[::1]:8790",
    ]) {
      const stream = await FrameStream.connect(baseUrl, {
        sessionId: "session-1",
        streamPath: "/stream",
        streamToken: "stream-secret",
        plannerToken: "planner-secret",
        protocol: "screen-frame-v1",
      });
      await stream.close();
    }
  } finally {
    if (originalDescriptor) {
      Object.defineProperty(globalThis, "WebSocket", originalDescriptor);
    } else {
      Reflect.deleteProperty(globalThis, "WebSocket");
    }
  }
  assert.equal(urls.length, 3);
  assert.ok(urls[0].startsWith("ws://localhost:8790/stream?"));
  assert.ok(urls[1].startsWith("ws://127.0.0.1:8790/stream?"));
  assert.ok(urls[2].startsWith("ws://[::1]:8790/stream?"));
});

test("frame streams preserve host source metadata", async () => {
  const originalDescriptor = Object.getOwnPropertyDescriptor(
    globalThis,
    "WebSocket",
  );
  const sent: Array<string | ArrayBufferLike | Blob | ArrayBufferView> = [];
  class FakeWebSocket extends EventTarget {
    static readonly OPEN = 1;
    readyState = 0;

    constructor(_url: string | URL) {
      super();
      queueMicrotask(() => {
        this.readyState = FakeWebSocket.OPEN;
        this.dispatchEvent(new Event("open"));
      });
    }

    send(data: string | ArrayBufferLike | Blob | ArrayBufferView): void {
      sent.push(data);
      if (typeof data !== "string") {
        queueMicrotask(() =>
          this.dispatchEvent(
            new MessageEvent("message", {
              data: JSON.stringify({ type: "screen.frame.accepted" }),
            }),
          ),
        );
      }
    }

    close(): void {
      this.readyState = 3;
    }
  }
  Object.defineProperty(globalThis, "WebSocket", {
    configurable: true,
    value: FakeWebSocket as unknown as typeof WebSocket,
  });
  try {
    const stream = await FrameStream.connect("http://127.0.0.1:8790", {
      sessionId: "session-1",
      streamPath: "/stream",
      streamToken: "stream-secret",
      plannerToken: "planner-secret",
      protocol: "screen-frame-v1",
    });
    await stream.sendFrame({
      frameId: "desktop-frame-1",
      capturedAtMs: 1_000,
      image: new Uint8Array([1, 2, 3]),
      metadata: {
        source_kind: "desktop_live_screen",
        source_width: 1_280,
        source_height: 720,
      },
    });
    await stream.close();
  } finally {
    if (originalDescriptor) {
      Object.defineProperty(globalThis, "WebSocket", originalDescriptor);
    } else {
      Reflect.deleteProperty(globalThis, "WebSocket");
    }
  }
  const header = JSON.parse(String(sent[0])) as {
    metadata: Record<string, unknown>;
  };
  assert.deepEqual(header.metadata, {
    source_kind: "desktop_live_screen",
    source_width: 1_280,
    source_height: 720,
  });
  assert.deepEqual(sent[1], new Uint8Array([1, 2, 3]));
});

test("decision retries preserve request id and attempts are never retried", async () => {
  const decisionRequests: string[] = [];
  let attemptCalls = 0;
  const fetchStub: typeof fetch = async (input, init) => {
    const url = String(input);
    if (url.endsWith("/decisions")) {
      const body = JSON.parse(String(init?.body)) as { request_id: string };
      decisionRequests.push(body.request_id);
      if (decisionRequests.length === 1) {
        return new Response(
          JSON.stringify({
            error: { code: "temporarily_unavailable", message: "retry" },
          }),
          { status: 503, headers: { "content-type": "application/json" } },
        );
      }
      return new Response(JSON.stringify({ request_id: body.request_id }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    attemptCalls += 1;
    return new Response(
      JSON.stringify({
        error: { code: "temporarily_unavailable", message: "retry" },
      }),
      { status: 503, headers: { "content-type": "application/json" } },
    );
  };

  const client = new VisualClient({
    baseUrl: "https://visual.invalid",
    apiToken: "stub-secret",
    maxRetries: 2,
    fetch: fetchStub,
  });
  const decision = await client.decide("session-1", "same-request-id");
  assert.equal(decision.request_id, "same-request-id");
  assert.deepEqual(decisionRequests, ["same-request-id", "same-request-id"]);

  await assert.rejects(
    client.recordAttempt("session-1", "decision-1"),
    (error: unknown) => {
      assert.ok(error instanceof VisualServiceError);
      assert.equal(error.status, 503);
      assert.equal(error.retryable, true);
      assert.equal(String(error).includes("stub-secret"), false);
      return true;
    },
  );
  assert.equal(attemptCalls, 1);
});

test("close retries accept unknown session only after an earlier attempt", async () => {
  let calls = 0;
  const fetchStub: typeof fetch = async () => {
    calls += 1;
    if (calls === 1) {
      return new Response(
        JSON.stringify({
          error: { code: "temporarily_unavailable", message: "retry" },
        }),
        { status: 503, headers: { "content-type": "application/json" } },
      );
    }
    return new Response(
      JSON.stringify({
        error: { code: "unknown_session", message: "session is missing" },
      }),
      { status: 404, headers: { "content-type": "application/json" } },
    );
  };
  const client = new VisualClient({
    baseUrl: "https://visual.example",
    apiToken: "host-token",
    maxRetries: 1,
    fetch: fetchStub,
  });

  assert.deepEqual(await client.closeSession("session-1"), { closed: true });
  assert.equal(calls, 2);
});

test("close does not suppress unknown session on its first attempt", async () => {
  let calls = 0;
  const fetchStub: typeof fetch = async () => {
    calls += 1;
    return new Response(
      JSON.stringify({
        error: { code: "unknown_session", message: "session is missing" },
      }),
      { status: 404, headers: { "content-type": "application/json" } },
    );
  };
  const client = new VisualClient({
    baseUrl: "https://visual.example",
    apiToken: "host-token",
    maxRetries: 1,
    fetch: fetchStub,
  });

  await assert.rejects(
    client.closeSession("session-1"),
    (error: unknown) =>
      error instanceof VisualServiceError &&
      error.code === "unknown_session" &&
      error.status === 404,
  );
  assert.equal(calls, 1);
});
