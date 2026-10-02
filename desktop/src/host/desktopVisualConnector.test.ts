import assert from "node:assert/strict";
import { test } from "node:test";
import {
  DesktopVisualConnector,
  type DesktopVisualConnectorOptions,
} from "./desktopVisualConnector.js";
import type { ScreenFrameMetadata } from "../shared/protocol.js";
import type { VisualSession } from "../../../sdks/typescript/src/index.js";

const SESSION: VisualSession = {
  sessionId: "session-1",
  streamPath: "/stream",
  streamToken: "stream-token",
  plannerToken: "planner-token",
  protocol: "screen-frame-v1",
};

function frame(): ScreenFrameMetadata {
  return {
    type: "screen.frame",
    frame_id: "frame-1",
    captured_at_ms: 1_000,
    encoding: "jpeg",
    video_source: "screen",
    width: 1_000,
    height: 500,
  };
}

test("desktop connector forwards the live frame through ActionBroker", async () => {
  const sentFrames: Array<Record<string, unknown>> = [];
  const attempts: Array<[string, string]> = [];
  const controls: Array<Record<string, unknown>> = [];
  let connector: DesktopVisualConnector;
  const decisions = [
    {
      decision_id: "decision-1",
      decision: {
        action: "click",
        target: { x: 250, y: 125 },
      },
    },
  ];
  const services = {
    host: {
      createSession: async () => SESSION,
      closeSession: async () => ({}),
      setTask: async () => ({}),
      decide: async () => ({}),
      recordAttempt: async (sessionId: string, decisionId: string) => {
        attempts.push([sessionId, decisionId]);
        return {};
      },
    },
    planner: () => ({
      createSession: async () => SESSION,
      closeSession: async () => ({}),
      setTask: async () => ({}),
      decide: async () => decisions.shift() ?? {},
      recordAttempt: async () => ({}),
    }),
    stream: async () => ({
      sendFrame: async (options: Record<string, unknown>) => {
        sentFrames.push(options);
        return {};
      },
      close: async () => {},
    }),
  };
  const options: DesktopVisualConnectorOptions = {
    baseUrl: "http://127.0.0.1:8765",
    hostToken: "host-token",
    task: "Click the visible control",
    capabilityManifestId: "desktop-v2",
    latestTurnId: () => "turn-1",
    log: () => {},
    services,
    dispatch: (control) => {
      controls.push(control);
      queueMicrotask(() => {
        connector.handleBrokerControl({
          type: "tool.response",
          call_id: control.call_id,
          content: { status: "done", verified: true },
        });
      });
      return true;
    },
  };
  connector = new DesktopVisualConnector(options);
  await connector.start();
  connector.observeFrame(frame(), Buffer.from("jpeg"));
  await eventually(() => attempts.length === 1);

  assert.equal(sentFrames.length, 1);
  assert.deepEqual(sentFrames[0].metadata, {
    source_kind: "desktop_live_screen",
    source_width: 1_000,
    source_height: 500,
  });
  assert.deepEqual(controls[0].tool_calls, [
    {
      name: "input",
      arguments: { action: "click", x: 0.25, y: 0.25 },
    },
  ]);
  assert.equal(controls[0].turn_id, "turn-1");
  assert.equal(controls[0].provenance, "mixed");
  assert.equal(controls[0].capability_manifest_id, "desktop-v2");
  assert.deepEqual(attempts, [["session-1", "decision-1"]]);
  await connector.close();
});

test("desktop connector keeps only the newest frame while an action runs", async () => {
  const frameIds: string[] = [];
  const attempts: string[] = [];
  let release: (() => void) | undefined;
  let connector: DesktopVisualConnector;
  const options: DesktopVisualConnectorOptions = {
    baseUrl: "http://127.0.0.1:8765",
    hostToken: "host-token",
    task: "Go back",
    capabilityManifestId: "desktop-v2",
    latestTurnId: () => "turn-1",
    log: () => {},
    services: {
      host: {
        createSession: async () => SESSION,
        closeSession: async () => ({}),
        setTask: async () => ({}),
        decide: async () => ({}),
        recordAttempt: async (_sessionId, decisionId) => {
          attempts.push(decisionId);
          return {};
        },
      },
      planner: () => ({
        createSession: async () => SESSION,
        closeSession: async () => ({}),
        setTask: async () => ({}),
        decide: async () => ({
          decision_id: `decision-${frameIds.at(-1)}`,
          decision: { action: "back" },
        }),
        recordAttempt: async () => ({}),
      }),
      stream: async () => ({
        sendFrame: async (options) => {
          frameIds.push(options.frameId);
          return {};
        },
        close: async () => {},
      }),
    },
    dispatch: (control) => {
      if (frameIds.length === 1) {
        release = () =>
          connector.handleBrokerControl({
            type: "tool.response",
            call_id: control.call_id,
            content: { status: "done" },
          });
      } else {
        queueMicrotask(() =>
          connector.handleBrokerControl({
            type: "tool.response",
            call_id: control.call_id,
            content: { status: "done" },
          }),
        );
      }
      return true;
    },
  };
  connector = new DesktopVisualConnector(options);
  await connector.start();
  connector.observeFrame(frame(), Buffer.from("one"));
  await eventually(() => release !== undefined);
  connector.observeFrame({ ...frame(), frame_id: "frame-2" }, Buffer.from("two"));
  connector.observeFrame({ ...frame(), frame_id: "frame-3" }, Buffer.from("three"));
  release?.();
  await eventually(() => attempts.length === 2);

  assert.deepEqual(frameIds, ["frame-1", "frame-3"]);
  await connector.close();
});

async function eventually(predicate: () => boolean): Promise<void> {
  const deadline = Date.now() + 1_000;
  while (!predicate()) {
    if (Date.now() >= deadline) throw new Error("condition was not met");
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}
