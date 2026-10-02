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
    apiToken: "sdk-test-token",
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
  assert.equal(JSON.stringify(client), "{}");
  assert.equal(client.toString(), "VisualClient");
  assert.equal(String(session).includes(session.streamToken), false);
  const stream = await FrameStream.connect(fixture.baseUrl, session);
  try {
    const image = await createJpeg();
    const accepted = await stream.sendFrame({
      frameId: "sdk-frame-1",
      capturedAtMs: 1_000,
      image,
    });
    assert.equal(accepted.type, "screen.frame.accepted");
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

  await client.setTask(session.sessionId, "click the control", ["click"]);
  const first = await client.decide(session.sessionId, "sdk-request-1");
  const repeated = await client.decide(session.sessionId, "sdk-request-1");
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
  const state = await client.state(session.sessionId);
  assert.ok(JSON.stringify(state.history).includes("click"));
  await client.resetTask(session.sessionId);
  await client.closeSession(session.sessionId);
  await assert.rejects(
    client.state(session.sessionId),
    (error: unknown) => error instanceof VisualServiceError,
  );
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
