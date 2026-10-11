import assert from "node:assert/strict";
import { once } from "node:events";
import { test } from "node:test";
import { WebSocketServer, type RawData } from "ws";
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

test("daemon playback cancellation is telemetry-only while local interruption sends break", async (t) => {
  const server = new WebSocketServer({ port: 0 });
  await once(server, "listening");
  const port = (server.address() as { port: number }).port;
  const connection = once(server, "connection");
  const session = new HostSession({
    runtimeUrl: `http://127.0.0.1:${port}`,
    hostId: "h1",
    chassis: "test",
    capabilities,
  });
  session.connect("s1");
  const socketOpen = once(session["duplex"]!, "open");
  const [socket, request] = await connection;
  assert.equal(new URL(request.url, "http://127.0.0.1").pathname, "/ws/duplex");
  await socketOpen;
  t.after(() => {
    session.disconnect("test_done");
    return new Promise<void>((resolve) => server.close(() => resolve()));
  });

  const wireMessages: string[] = [];
  socket.on("message", (data: RawData, isBinary: boolean) => {
    if (!isBinary) wireMessages.push(data.toString());
  });
  const waitForMessageCount = async (count: number) => {
    while (wireMessages.length < count) await once(socket, "message");
  };
  const daemonMessages = waitForMessageCount(1);
  session.emit({
    type: "playback.cancelled",
    playback_id: "pb-daemon",
    output_epoch: 1,
    reason: "daemon_cancel",
    ts_ms: Date.now(),
  });
  await daemonMessages;
  const daemonControls = wireMessages.map((message) => JSON.parse(message) as {
    type: string;
    reason?: string;
    event?: { type: string; reason?: string };
  });
  assert.deepEqual(daemonControls.map((control) => control.type), ["host.event"]);
  assert.equal(daemonControls[0].event?.type, "playback.cancelled");
  assert.equal(daemonControls[0].event?.reason, "daemon_cancel");

  const localMessages = waitForMessageCount(3);
  session.emit({
    type: "playback.cancelled",
    playback_id: "pb-local",
    output_epoch: 2,
    reason: "user_interrupt",
    ts_ms: Date.now(),
  });
  await localMessages;
  const controls = wireMessages.map((message) => JSON.parse(message) as {
    type: string;
    reason?: string;
    event?: { type: string; reason?: string };
  });
  assert.deepEqual(
    controls.filter((control) => control.type === "break"),
    [{ type: "break", reason: "user_interrupt" }],
  );

});

test("ending a call without stopping keeps the daemon session: call.ended goes out, stop does not", async () => {
  const port = await deadPort();
  const session = new HostSession({
    runtimeUrl: `http://127.0.0.1:${port}`,
    hostId: "h1",
    chassis: "test",
    capabilities,
  });
  session.connect("s1");
  // Not connected (dead port), so every control queues in the duplex outbox in order.
  const queued = () =>
    (session["duplex"]!["outbox"] as Array<string | Buffer>)
      .filter((item): item is string => typeof item === "string")
      .map((item) => JSON.parse(item) as { type: string; event?: { type: string; reason?: string } });
  session.startCall();
  session.endCall("live_ended", { stop: false });
  let types = queued().map((c) => (c.type === "host.event" ? `host.event:${c.event?.type}` : c.type));
  assert.deepEqual(types, ["host.event:call.started", "host.event:call.ended"]);
  assert.equal(queued().at(-1)?.event?.reason, "live_ended");
  // The default still ends the session, as the renderer's stop path relies on.
  session.endCall("client_stop");
  types = queued().map((c) => (c.type === "host.event" ? `host.event:${c.event?.type}` : c.type));
  assert.deepEqual(types.slice(-2), ["host.event:call.ended", "stop"]);
  session.disconnect("test_done");
});

test("the desktop offers its actions on connect, and a reconnect carries the resume token", async () => {
  const server = new WebSocketServer({ port: 0 });
  await once(server, "listening");
  const port = (server.address() as { port: number }).port;
  const urls: string[] = [];
  server.on("connection", (socket, request) => {
    urls.push(request.url ?? "");
    if (urls.length === 1) {
      socket.send(JSON.stringify({ type: "ready", session_id: "s1", resume_token: "tok-123" }));
    }
  });
  const session = new HostSession({
    runtimeUrl: `http://127.0.0.1:${port}`,
    hostId: "h1",
    chassis: "test",
    capabilities,
    hostTools: { names: ["open", "files"], version: "desktop-v1" },
  });
  session.connect("s1");
  await sleep(150);
  // The same session again, as after a dropped connection.
  session.connect("s1");
  await sleep(150);
  session.disconnect("test_done");
  await new Promise<void>((r) => server.close(() => r()));
  assert.equal(urls[0], "/ws/duplex?session_id=s1&host_tools=open%2Cfiles&host_tools_version=desktop-v1");
  assert.equal(
    urls.find((u) => u.includes("resume_token")),
    "/ws/duplex?session_id=s1&host_tools=open%2Cfiles&host_tools_version=desktop-v1&resume_token=tok-123",
  );
});
