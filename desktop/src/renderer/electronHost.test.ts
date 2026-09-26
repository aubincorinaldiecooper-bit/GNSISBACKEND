import assert from "node:assert/strict";
import { test } from "node:test";
import type { LiveEvent } from "@gnsis/ui";
import type { GnsisBridge, ScreenUpdate } from "./bridge.js";
import { ElectronLiveHost, type Devices } from "./electronHost.js";

/**
 * The host adapter is exercised with a fake preload bridge and fake devices,
 * so what it promises the UI (words, speaking, cuts, spoken turns, mute
 * semantics) is checked without a window, a microphone or a daemon.
 */

class FakeBridge implements GnsisBridge {
  controls: unknown[] = [];
  hostEvents: unknown[] = [];
  logs: string[] = [];
  startCalls = 0;
  private handlers: Record<string, ((...a: any[]) => void) | undefined> = {};
  mediaPermissions = async () => ({ microphone: "granted", camera: "denied", screen: "unknown" });
  requestPermission = async () => "granted";
  sendControl = (c: unknown) => { this.controls.push(c); };
  sendHostEvent = (e: unknown) => { this.hostEvents.push(e); };
  hostLog = (l: string) => { this.logs.push(l); };
  startCall = () => { this.startCalls++; };
  sendAudioFrame = () => {};
  sendScreenFrame = () => {};
  callTool = async () => ({});
  onControl = (fn: (c: unknown) => void) => { this.handlers.control = fn; };
  onAudio = (fn: (pcm: Uint8Array) => void) => { this.handlers.audio = fn; };
  onClosed = (fn: (code: number, reason: string) => void) => { this.handlers.closed = fn; };
  onScreen = (fn: (u: ScreenUpdate) => void) => { this.handlers.screen = fn; };
  onInterrupted = (fn: () => void) => { this.handlers.interrupted = fn; };
  // the main process talking to us
  control(c: unknown) { this.handlers.control?.(c); }
  audio(pcm: Uint8Array) { this.handlers.audio?.(pcm); }
  closed(code: number) { this.handlers.closed?.(code, ""); }
  screen(u: ScreenUpdate) { this.handlers.screen?.(u); }
  interrupted() { this.handlers.interrupted?.(); }
}

function fakeDevices(opts: { micError?: Error; visionError?: Error } = {}) {
  const calls: string[] = [];
  const devices: Devices & { calls: string[]; micActive: boolean; playing: boolean } = {
    calls,
    micActive: false,
    playing: false,
    mic: {
      get active() { return devices.micActive; },
      onLevel: undefined,
      async start() { calls.push("mic.start"); if (opts.micError) throw opts.micError; devices.micActive = true; },
      stop() { calls.push("mic.stop"); devices.micActive = false; },
    },
    playback: {
      get speaking() { return devices.playing; },
      onSpeaking: undefined,
      level: () => 0.4,
      play(pcm: Uint8Array) { calls.push(`play:${pcm.byteLength}`); devices.playing = true; devices.playback.onSpeaking?.(true); },
      cancel(reason?: string) { calls.push(`cancel:${reason}`); const n = devices.playing ? 1 : 0; devices.playing = false; devices.playback.onSpeaking?.(false); return n; },
    },
    vision: {
      active: false,
      current: null,
      onAccepted: undefined,
      onEnded: undefined,
      applyChannel() { calls.push("vision.applyChannel"); },
      handleScreenControl(c: unknown) { calls.push(`vision.control:${(c as { type?: string }).type}`); },
      async share(source) { calls.push(`vision.share:${source}`); if (opts.visionError) throw opts.visionError; },
      stop() { calls.push("vision.stop"); },
    },
  };
  return devices;
}

function harness(opts: { micError?: Error; visionError?: Error; now?: () => number } = {}) {
  const bridge = new FakeBridge();
  const devices = fakeDevices(opts);
  const events: LiveEvent[] = [];
  const host = new ElectronLiveHost(bridge, devices, (l) => bridge.logs.push(l), {
    now: opts.now,
    speechThreshold: 0.2,
    speechHangoverMs: 300,
    setInterval: (() => 0) as unknown as typeof setInterval,
    clearInterval: (() => {}) as unknown as typeof clearInterval,
  });
  host.attach();
  host.subscribe((e) => events.push(e));
  return { bridge, devices, events, host };
}

const tick = () => new Promise((r) => setTimeout(r, 0));

test("the runtime's text chunks become the reply's words, with the end and interruption flags", async () => {
  const { bridge, events } = harness();
  await tick();
  bridge.control({ type: "chunk", text: "Hi there", end_of_turn: false, interrupted: false, is_listen: false });
  bridge.control({ type: "chunk", text: "", end_of_turn: false, interrupted: false, is_listen: true });
  bridge.control({ type: "chunk", text: ", how are you?", end_of_turn: true, interrupted: false });
  bridge.control({ type: "chunk", text: "", end_of_turn: false, interrupted: true });
  const words = events.filter((e) => e.type === "agent.text");
  assert.deepEqual(words, [
    { type: "agent.text", text: "Hi there", endOfTurn: false, interrupted: false },
    { type: "agent.text", text: ", how are you?", endOfTurn: true, interrupted: false },
    { type: "agent.text", text: "", endOfTurn: false, interrupted: true },
  ]);
  assert.ok(bridge.logs.some((l) => l.startsWith("<- chunk ")), "controls still reach the host log");
});

test("the link state is what the daemon last said, and a late subscriber hears it", async () => {
  const { bridge, events, host } = harness();
  await tick();
  assert.deepEqual(events[0], { type: "link", state: "connecting", detail: undefined });
  bridge.control({ type: "runtime.status", status: "loading" });
  bridge.control({ type: "ready", session_id: "s1" });
  const late: LiveEvent[] = [];
  host.subscribe((e) => late.push(e));
  await tick();
  assert.deepEqual(late, [{ type: "link", state: "ready", detail: undefined }]);
  bridge.closed(1006);
  assert.deepEqual(events.at(-1), { type: "link", state: "closed", detail: "The connection to GNSIS closed." });
  bridge.control({ type: "error", message: "boom", fatal: true });
  assert.deepEqual(events.at(-1), { type: "link", state: "error", detail: "The session could not start." });
});

test("audio plays as it arrives; the daemon's cancel and the global shortcut both cut it", async () => {
  const { bridge, devices, events } = harness();
  await tick();
  bridge.audio(new Uint8Array(480));
  assert.deepEqual(devices.calls, ["play:480"]);
  assert.deepEqual(events.at(-1), { type: "agent.speaking", speaking: true });
  bridge.control({ type: "playback.cancel", generation_id: 3 });
  assert.equal(devices.calls.at(-1), "cancel:daemon_cancel");
  assert.deepEqual(events.slice(-2), [
    { type: "agent.speaking", speaking: false },
    { type: "agent.cut", reason: "daemon_cancel" },
  ]);
  bridge.interrupted();
  assert.equal(devices.calls.at(-1), "cancel:global_shortcut");
  assert.deepEqual(events.at(-1), { type: "agent.cut", reason: "global_shortcut" });
});

test("starting live starts one call and the microphone; mute releases capture only", async () => {
  const { bridge, devices, events, host } = harness();
  await tick();
  await host.startLive();
  assert.equal(bridge.startCalls, 1);
  assert.deepEqual(devices.calls, ["mic.start"]);
  assert.deepEqual(events.at(-1), { type: "mic", state: "on" });
  await host.setMuted(true);
  assert.deepEqual(devices.calls, ["mic.start", "mic.stop"]);
  await host.setMuted(false);
  assert.deepEqual(devices.calls, ["mic.start", "mic.stop", "mic.start"]);
  assert.equal(bridge.startCalls, 1, "unmuting is not a new call");
  assert.ok(!bridge.controls.some((c) => (c as { type?: string }).type === "stop"), "mute never sends stop");
  devices.playing = true;
  await host.endLive();
  assert.equal(devices.calls.at(-2), "mic.stop");
  assert.equal(devices.calls.at(-1), "cancel:live_ended");
  assert.deepEqual(events.at(-1), { type: "mic", state: "off" });
  await host.startLive();
  assert.equal(bridge.startCalls, 2, "each live session is a new call");
});

test("a refused microphone is reported in plain words and live does not start", async () => {
  const err = Object.assign(new Error("Permission denied"), { name: "NotAllowedError" });
  const { bridge, events, host } = harness({ micError: err });
  await tick();
  await assert.rejects(host.startLive(), /The microphone was not allowed/);
  assert.equal(bridge.startCalls, 1);
  const mic = events.find((e) => e.type === "mic");
  assert.equal(mic && mic.type === "mic" && mic.state, "denied");
  assert.ok(!bridge.logs.some((l) => l === "live on"));
});

test("microphone energy marks where the person's turns start and stop", async () => {
  let now = 10_000;
  const { devices, events, host } = harness({ now: () => now });
  await tick();
  await host.startLive();
  const level = (l: number, dt: number) => { now += dt; devices.mic.onLevel?.(l); };
  level(0.05, 50);
  level(0.5, 50);
  assert.deepEqual(events.filter((e) => e.type === "user.speech"), [{ type: "user.speech", state: "start" }]);
  level(0.6, 50);
  level(0.05, 50); // quiet begins
  level(0.05, 100);
  assert.equal(events.filter((e) => e.type === "user.speech").length, 1, "a short pause is not the end");
  level(0.05, 250); // 350 ms of quiet: the turn ended when the quiet began
  const speech = events.filter((e) => e.type === "user.speech");
  assert.equal(speech.length, 2);
  assert.deepEqual(speech[1], { type: "user.speech", state: "end", ms: 100 });
  // While muted the microphone is closed, so no levels arrive; if one did, it is ignored.
  await host.setMuted(true);
  level(0.9, 50);
  assert.equal(events.filter((e) => e.type === "user.speech").length, 2);
  // Ending live mid-sentence closes the turn.
  await host.setMuted(false);
  level(0.9, 50);
  assert.equal(events.filter((e) => e.type === "user.speech").length, 3);
  await host.endLive();
  assert.equal(events.filter((e) => e.type === "user.speech").length, 4);
});

test("the visual sense reports starting, then on only once the daemon accepts a frame", async () => {
  const { bridge, devices, events, host } = harness();
  await tick();
  await host.startVision("screen");
  assert.deepEqual(events.at(-1), { type: "vision", source: "screen", state: "starting" });
  assert.equal(devices.calls.at(-1), "vision.share:screen");
  bridge.screen({ channel: { token: "t", recommended_frame_rate: 1 } });
  assert.equal(devices.calls.at(-1), "vision.applyChannel");
  devices.vision.onAccepted?.("screen");
  devices.vision.onAccepted?.("screen");
  assert.equal(events.filter((e) => e.type === "vision" && e.state === "on").length, 1);
  await host.stopVision();
  assert.equal(devices.calls.at(-1), "vision.stop");
  assert.deepEqual(events.at(-1), { type: "vision", source: null, state: "off" });
});

test("a refused screen share is a denied state with instructions, not a crash", async () => {
  const err = Object.assign(new Error("Permission denied"), { name: "NotAllowedError" });
  const { events, host } = harness({ visionError: err });
  await tick();
  await assert.rejects(host.startVision("screen"), /Screen recording was not allowed/);
  const last = events.at(-1);
  assert.ok(last && last.type === "vision" && last.state === "denied" && last.source === "screen");
});
