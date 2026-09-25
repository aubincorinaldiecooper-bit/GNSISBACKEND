import assert from "node:assert/strict";
import { once } from "node:events";
import { test } from "node:test";
import { WebSocketServer } from "ws";
import { HostSession } from "./hostSession.js";
import type { ScreenChannelConfig } from "../shared/protocol.js";

/**
 * PR-H transport regression: socket `error` events must surface as control
 * telemetry instead of uncaught exceptions in the Electron main process.
 */

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

const capabilities = {
  mic: true,
  camera: true,
  screen: true,
  playback_ack: true,
  global_shortcuts: false,
  notifications: false,
};

const deadPort = async (): Promise<number> => {
  const probe = new WebSocketServer({ port: 0 });
  await once(probe, "listening");
  const port = (probe.address() as { port: number }).port;
  await new Promise<void>((r) => probe.close(() => r()));
  return port;
};

const channel: ScreenChannelConfig = {
  enabled: true,
  path: "/ws/screen",
  token: "tok",
};

test("duplex socket errors surface as transport.error, not uncaught exceptions", async () => {
  const port = await deadPort();
  const controls: unknown[] = [];
  const session = new HostSession({
    runtimeUrl: `http://127.0.0.1:${port}`,
    hostId: "h1",
    chassis: "test",
    capabilities,
    onControl: (c) => controls.push(c),
  });
  session.connect("s1");
  await sleep(200);
  const err = controls.find(
    (c) => (c as { type?: string; channel?: string }).type === "transport.error" &&
      (c as { channel?: string }).channel === "duplex",
  );
  assert.ok(err, "expected a duplex transport.error control event");
  session.disconnect("test_done");
});

test("screen socket errors surface via onScreen, not uncaught exceptions", async () => {
  const port = await deadPort();
  const updates: unknown[] = [];
  const session = new HostSession({
    runtimeUrl: `http://127.0.0.1:${port}`,
    hostId: "h1",
    chassis: "test",
    capabilities,
    onScreen: (u) => updates.push(u),
  });
  session.connect("s1");
  // Screen transport only opens once the daemon-issued token arrives.
  session["screen"]?.applyChannel(channel);
  await sleep(200);
  const err = updates.find((u) => {
    const c = (u as { control?: { type?: string; channel?: string } }).control;
    return c?.type === "transport.error" && c?.channel === "screen";
  });
  assert.ok(err, "expected a screen transport.error update");
  session.disconnect("test_done");
});
