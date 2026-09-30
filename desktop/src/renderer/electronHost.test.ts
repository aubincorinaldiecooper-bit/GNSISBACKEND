import assert from "node:assert/strict";
import { test } from "node:test";
import type { LiveEvent } from "@gnsis/ui";
import type { GnsisBridge, LinkState, ScreenUpdate, TurnResult } from "./bridge.js";
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
  endCalls: string[] = [];
  reconnects = 0;
  /** What the main process would answer about the link when the page loads. */
  state: LinkState = { ready: { type: "ready", session_id: "s1" }, connected: true, closed: false };
  private handlers: Record<string, ((...a: any[]) => void) | undefined> = {};
  mediaPermissions = async () => ({ microphone: "granted", camera: "denied", screen: "unknown" });
  requestPermission = async () => "granted";
  sendControl = (c: unknown) => { this.controls.push(c); };
  sendHostEvent = (e: unknown) => { this.hostEvents.push(e); };
  turns: string[] = [];
  /** What the main process answers when a typed turn is sent. */
  turnResult: TurnResult = { ok: true, turnId: "typed-1" };
  sendTurn = async (text: string) => { this.turns.push(text); return this.turnResult; };
  hostLog = (l: string) => { this.logs.push(l); };
  linkState = async () => this.state;
  reconnect = () => { this.reconnects++; };
  startCall = () => { this.startCalls++; };
  endCall = (reason: string) => { this.endCalls.push(reason); };
  sendAudioFrame = () => {};
  sendScreenFrame = () => {};
  callTool = async () => ({});
  onControl = (fn: (c: unknown) => void) => { this.handlers.control = fn; };
  onAudio = (fn: (pcm: Uint8Array) => void) => { this.handlers.audio = fn; };
  onClosed = (fn: (code: number, reason: string) => void) => { this.handlers.closed = fn; };
  onScreen = (fn: (u: ScreenUpdate) => void) => { this.handlers.screen = fn; };
  onInterrupted = (fn: () => void) => { this.handlers.interrupted = fn; };
  onAction = (fn: (update: import("./bridge.js").ActionUpdate) => void) => { this.handlers.action = fn as (u: unknown) => void; };
  menuBar = false;
  faces: string[] = [];
  hides = 0;
  setMenuBarFace = (png: string) => { this.faces.push(png); };
  hideToMenuBar = () => { this.hides++; };
  onMenuBar = (fn: (m: unknown) => void) => { this.handlers.menubar = fn; };
  // the main process talking to us
  control(c: unknown) { this.handlers.control?.(c); }
  audio(pcm: Uint8Array) { this.handlers.audio?.(pcm); }
  closed(code: number) { this.handlers.closed?.(code, ""); }
  screen(u: ScreenUpdate) { this.handlers.screen?.(u); }
  interrupted() { this.handlers.interrupted?.(); }
  action(u: import("./bridge.js").ActionUpdate) { this.handlers.action?.(u); }
  menubar(m: unknown) { this.handlers.menubar?.(m); }
}

function fakeDevices(opts: { micError?: Error; visionError?: Error; micGate?: Promise<void> } = {}) {
  const calls: string[] = [];
  const devices: Devices & { calls: string[]; micActive: boolean; playing: boolean } = {
    calls,
    micActive: false,
    playing: false,
    mic: {
      get active() { return devices.micActive; },
      onLevel: undefined,
      async start() {
        calls.push("mic.start");
        if (opts.micGate) await opts.micGate;
        if (opts.micError) throw opts.micError;
        devices.micActive = true;
      },
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

function harness(opts: { micError?: Error; visionError?: Error; micGate?: Promise<void>; now?: () => number; ready?: boolean; state?: LinkState; readyTimeoutMs?: number; menuBar?: boolean } = {}) {
  const bridge = new FakeBridge();
  bridge.menuBar = opts.menuBar === true;
  if (opts.ready === false) bridge.state = { ready: null, connected: false, closed: false };
  if (opts.state) bridge.state = opts.state;
  const devices = fakeDevices(opts);
  const events: LiveEvent[] = [];
  const host = new ElectronLiveHost(bridge, devices, (l) => bridge.logs.push(l), {
    now: opts.now,
    speechThreshold: 0.2,
    speechHangoverMs: 300,
    readyTimeoutMs: opts.readyTimeoutMs,
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
  // The last speech unit of a reply also closes it, in case the text never carried the flag.
  bridge.control({ type: "audio.done", generation_id: 2, unit_id: 4, end_of_turn: true });
  bridge.control({ type: "audio.done", generation_id: 3, unit_id: 1, end_of_turn: false });
  const words = events.filter((e) => e.type === "agent.text");
  assert.deepEqual(words, [
    { type: "agent.text", text: "Hi there", endOfTurn: false, interrupted: false },
    { type: "agent.text", text: ", how are you?", endOfTurn: true, interrupted: false },
    { type: "agent.text", text: "", endOfTurn: false, interrupted: true },
    { type: "agent.text", text: "", endOfTurn: true, interrupted: false },
  ]);
  assert.ok(bridge.logs.some((l) => l.startsWith("<- chunk ")), "controls still reach the host log");
});

test("starting live waits for the runtime to be ready; End cancels the wait", async () => {
  const { bridge, devices, host } = harness({ ready: false });
  await tick();
  const attempt = host.startLive();
  await tick();
  assert.equal(bridge.startCalls, 0, "no call and no microphone before the runtime is ready");
  assert.deepEqual(devices.calls, []);
  bridge.control({ type: "ready", session_id: "s1" });
  await attempt;
  assert.equal(bridge.startCalls, 1);
  assert.deepEqual(devices.calls, ["mic.start"]);
  await host.endLive();
  // A second attempt, abandoned by End while still connecting: nothing opens, nothing throws.
  bridge.closed(1006);
  const abandoned = host.startLive();
  await tick();
  assert.equal(bridge.reconnects, 1, "a closed link is reopened first");
  await host.endLive();
  await abandoned;
  assert.equal(bridge.startCalls, 1);
  assert.equal(devices.calls.filter((c) => c === "mic.start").length, 1);
});

test("a runtime that never becomes ready is reported in plain words, not waited on forever", async () => {
  const { bridge, host } = harness({ ready: false, readyTimeoutMs: 30 });
  await tick();
  await assert.rejects(host.startLive(), /GNSIS could not be reached/);
  assert.equal(bridge.startCalls, 0);
});

test("a ready that went by before the page loaded is picked up from the main process", async () => {
  const { events } = harness();
  await tick();
  assert.deepEqual(events.at(-1), { type: "link", state: "ready", detail: undefined });
  const closedBefore = harness({ state: { ready: { type: "ready" }, connected: false, closed: true } });
  await tick();
  assert.deepEqual(closedBefore.events.at(-1), { type: "link", state: "closed", detail: "The connection to GNSIS closed." });
});

test("the link state is what the daemon last said, and a late subscriber hears it", async () => {
  const { bridge, events, host } = harness({ ready: false });
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

test("audio plays while live; the daemon's cancel and the global shortcut both cut it", async () => {
  const { bridge, devices, events, host } = harness();
  await tick();
  await host.startLive();
  bridge.control({ type: "audio.chunk", generation_id: 3, unit_id: 1 });
  bridge.audio(new Uint8Array(480));
  assert.equal(devices.calls.at(-1), "play:480");
  assert.deepEqual(events.at(-1), { type: "agent.speaking", speaking: true });
  bridge.control({ type: "playback.cancel", generation_id: 4, cancelled_generation_id: 3 });
  assert.equal(devices.calls.at(-1), "cancel:daemon_cancel");
  assert.deepEqual(events.slice(-2), [
    { type: "agent.speaking", speaking: false },
    { type: "agent.cut", reason: "daemon_cancel" },
  ]);
  bridge.interrupted();
  assert.equal(devices.calls.at(-1), "cancel:global_shortcut");
  assert.deepEqual(events.at(-1), { type: "agent.cut", reason: "global_shortcut" });
});

test("audio from a cancelled generation, or after live ended, is dropped and logged, never played", async () => {
  const { bridge, devices, host } = harness();
  await tick();
  await host.startLive();
  const chunk = (gen: number, bytes: number) => {
    bridge.control({ type: "audio.chunk", generation_id: gen, unit_id: 1 });
    bridge.audio(new Uint8Array(bytes));
  };
  chunk(3, 100);
  assert.equal(devices.calls.at(-1), "play:100");
  // The daemon cancels generation 3; a chunk of it still in flight lands afterwards.
  bridge.control({ type: "playback.cancel", generation_id: 4, cancelled_generation_id: 3 });
  chunk(3, 200);
  assert.equal(devices.calls.at(-1), "cancel:daemon_cancel", "the late chunk was not scheduled");
  assert.ok(bridge.logs.some((l) => l.startsWith("stale audio dropped gen=3 bytes=200 reason=cancelled_generation")));
  // The next generation plays.
  chunk(4, 300);
  assert.equal(devices.calls.at(-1), "play:300");
  // The person ends live while generation 4 is still streaming: what arrives next is refused.
  await host.endLive();
  chunk(4, 400);
  chunk(5, 500);
  assert.ok(!devices.calls.includes("play:400") && !devices.calls.includes("play:500"), `nothing played after end: ${devices.calls}`);
  assert.ok(bridge.logs.some((l) => l.startsWith("stale audio dropped gen=5 bytes=500 reason=not_live")));
  // A new live session plays again, and the generation cut locally stays retired.
  await host.startLive();
  chunk(4, 600);
  chunk(6, 700);
  assert.ok(!devices.calls.includes("play:600"), "generation 4 was cut when live ended");
  assert.equal(devices.calls.at(-1), "play:700");
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
  assert.deepEqual(bridge.endCalls, ["live_ended"], "the call closes on the timeline without stopping the session");
  await host.startLive();
  assert.equal(bridge.startCalls, 2, "each live session is a new call");
  assert.equal(bridge.endCalls.length, 1);
});

test("a refused microphone is reported in plain words and live does not start", async () => {
  const err = Object.assign(new Error("Permission denied"), { name: "NotAllowedError" });
  const { bridge, events, host } = harness({ micError: err });
  await tick();
  await assert.rejects(host.startLive(), /The microphone was not allowed/);
  assert.equal(bridge.startCalls, 1);
  assert.deepEqual(bridge.endCalls, ["mic_failed"], "a call that never got a microphone is closed, not left open");
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

test("End pressed while the microphone is still opening: it closes again and live never starts", async () => {
  let open!: () => void;
  const micGate = new Promise<void>((resolve) => (open = resolve));
  const { bridge, devices, events, host } = harness({ micGate });
  await tick();
  const attempt = host.startLive();
  await tick();
  assert.deepEqual(devices.calls, ["mic.start"], "the microphone is being opened");
  await host.endLive();
  open();
  await attempt;
  assert.deepEqual(devices.calls, ["mic.start", "mic.stop"], "it is closed again as soon as it opens");
  assert.equal(devices.micActive, false);
  assert.deepEqual(bridge.endCalls, ["cancelled_while_starting"], "the call it opened is closed");
  assert.ok(!events.some((e) => e.type === "mic" && e.state === "on"), "the UI is never told the microphone is on");
  assert.ok(bridge.logs.some((l) => l.includes("cancelled while the microphone was opening")));
});

test("mute again, or End, while unmuting is still opening the microphone: it is closed as soon as it opens", async () => {
  for (const second of ["mute", "end"] as const) {
    const opts: { micGate?: Promise<void> } = {};
    const { bridge, devices, events, host } = harness(opts);
    await tick();
    await host.startLive();
    await host.setMuted(true);
    assert.equal(devices.micActive, false);
    let open!: () => void;
    opts.micGate = new Promise<void>((resolve) => (open = resolve));
    const unmute = host.setMuted(false);
    await tick();
    // The second click lands while the microphone is still opening.
    if (second === "mute") await host.setMuted(true);
    else await host.endLive();
    open();
    await unmute;
    assert.equal(devices.micActive, false, `after ${second}, nothing is capturing`);
    assert.equal(devices.calls.at(-1), "mic.stop");
    assert.ok(bridge.logs.includes("unmute abandoned: muted or ended while the microphone opened"));
    if (second === "end") assert.ok(events.some((e) => e.type === "mic" && e.state === "off"));
  }
});

test("End while connecting, then start again: the second attempt waits for the first to unwind", async () => {
  let open!: () => void;
  const micGate = new Promise<void>((resolve) => (open = resolve));
  const { bridge, devices, events, host } = harness({ micGate });
  await tick();
  const first = host.startLive();
  await tick();
  await host.endLive();
  const second = host.startLive();
  await tick();
  assert.deepEqual(devices.calls, ["mic.start"], "the second attempt does not touch the microphone while the first still holds it");
  open();
  await first;
  await second;
  assert.deepEqual(devices.calls, ["mic.start", "mic.stop", "mic.start"]);
  assert.equal(devices.micActive, true, "the second attempt ends up live");
  assert.equal(bridge.startCalls, 2);
  assert.deepEqual(events.filter((e) => e.type === "mic").map((e) => (e as { state: string }).state), ["on"]);
});

test("the log tells a live start apart at every stage: requested, waiting, cancelled", async () => {
  const { bridge, host } = harness({ ready: false });
  await tick();
  const attempt = host.startLive();
  await tick();
  await host.endLive();
  await attempt;
  assert.ok(bridge.logs.includes("live requested"));
  assert.ok(bridge.logs.includes("live: waiting for the runtime to be ready"));
  assert.ok(bridge.logs.includes("live: cancelled while connecting"));
  assert.equal(bridge.startCalls, 0);
});

test("typing is offered only when the runtime says it answers typed turns", async () => {
  const quiet = harness();
  await tick();
  assert.equal(quiet.host.capabilities().text, false, "a runtime that only records typed turns does not get typing");
  await assert.rejects(quiet.host.sendText("Open YouTube"), /Typing isn’t connected/);
  assert.deepEqual(quiet.bridge.turns, [], "nothing is sent");

  const { bridge, host, events } = harness({ ready: false });
  await tick();
  bridge.control({ type: "ready", session_id: "s1", typed_turns: true });
  assert.equal(host.capabilities().text, true);
  assert.deepEqual(events.at(-1), { type: "link", state: "ready", detail: undefined }, "the UI hears the link change, and re-reads what it can do");
  await host.sendText("  Open YouTube and search Andrew Tate  ");
  assert.deepEqual(bridge.turns, ["Open YouTube and search Andrew Tate"], "the words go to the main process, trimmed");
  assert.ok(bridge.logs.some((l) => l === "typed turn typed-1 accepted"));

  bridge.turnResult = { ok: false, reason: "GNSIS didn’t confirm it got your message. Try again in a moment.", unconfirmed: true };
  await assert.rejects(host.sendText("Again"), (e: Error & { unconfirmed?: boolean }) => /didn’t confirm/.test(e.message) && e.unconfirmed === true);
  await assert.rejects(host.sendText("   "), /nothing to send/);

  // A link that ends takes typing with it, until the next ready offers it again.
  bridge.control({ type: "session.done" });
  assert.equal(host.capabilities().text, false, "a finished session does not keep typing on");
  assert.deepEqual(events.at(-1), { type: "link", state: "closed", detail: "The session ended." });
  bridge.control({ type: "ready", session_id: "s1", typed_turns: true });
  assert.equal(host.capabilities().text, true);
  bridge.control({ type: "transport.error" });
  assert.equal(host.capabilities().text, false, "nor does a lost connection");
  bridge.control({ type: "ready", session_id: "s1" });
  assert.equal(host.capabilities().text, false, "a runtime that does not say it answers typed turns gets none");

  const late = harness({ state: { ready: { type: "ready", typed_turns: true }, connected: true, closed: false } });
  await tick();
  assert.equal(late.host.capabilities().text, true, "a ready that went by before the page loaded counts too");
});

test("the host log keeps a control's shape, never its credentials or its words", async () => {
  const { bridge } = harness({ ready: false });
  await tick();
  const token = "rt_5c1c2c7e4b6a4d0f9e8a7b6c5d4e3f2a1b0c9d8";
  bridge.control({ type: "ready", session_id: "host-42", resume_token: token, screen: { token: "scr_secret" }, typed_turns: true });
  bridge.control({ type: "tool.call", call_id: "call_0123456789abcdef", tool_calls: [{ name: "input", arguments: { action: "type", text: "Andrew Tate" } }] });
  bridge.control({ type: "chunk", text: "Searching YouTube for Andrew Tate now.", generation: 3 });
  const log = bridge.logs.join("\n");
  assert.ok(!log.includes(token) && !log.includes("scr_secret"), "no credential reaches the log");
  assert.ok(!log.includes("Andrew Tate"), "no words reach the log");
  assert.ok(log.includes('"session_id":"host-42"') && log.includes('"resume_token":"<redacted>"'));
  assert.ok(log.includes('"name":"input"') && log.includes('"arguments":"<'), "the tool's name stays; its arguments become a length");
  assert.ok(log.includes('"text":"<38 chars>"') && log.includes('"generation":3'));
});

test("what GNSIS does on the computer reaches the UI as it happens, without its words in the log", async () => {
  const { bridge, events } = harness();
  await tick();
  bridge.action({ callId: "c1", state: "working", text: "Open youtube.com in a new tab in Google Chrome" });
  bridge.action({ callId: "c1", state: "done", text: "Google Chrome opened a new tab at youtube.com." });
  assert.deepEqual(events.filter((e) => e.type === "action"), [
    { type: "action", state: "working", text: "Open youtube.com in a new tab in Google Chrome" },
    { type: "action", state: "done", text: "Google Chrome opened a new tab at youtube.com." },
  ]);
  assert.ok(bridge.logs.includes("action c1 working") && bridge.logs.includes("action c1 done"));
  assert.ok(!bridge.logs.some((l) => l.includes("youtube")));
});

test("unmute, End and Talk again in quick succession: the new call keeps its microphone", async () => {
  const opts: { micGate?: Promise<void> } = {};
  const { devices, events, host } = harness(opts);
  await tick();
  await host.startLive();
  await host.setMuted(true);
  let open!: () => void;
  opts.micGate = new Promise<void>((resolve) => (open = resolve));
  const unmute = host.setMuted(false);
  await tick();
  await host.endLive();
  // The voice button again, while the unmute is still opening the microphone.
  const again = host.startLive();
  await tick();
  assert.equal(devices.calls.filter((c) => c === "mic.start").length, 2, "the new call waits: it has not opened the microphone yet");
  open();
  await unmute;
  await again;
  assert.equal(devices.micActive, true, "the new call is capturing");
  assert.equal(devices.calls.at(-1), "mic.start", "nothing released it after it opened");
  const lastMic = events.filter((e) => e.type === "mic").at(-1) as { state: string } | undefined;
  assert.equal(lastMic?.state, "on");
});

test("the screen view gets exactly the picture being shared, and nothing once sharing stops", async () => {
  const { devices, host } = harness();
  assert.equal(host.visionStream(), null, "nothing shared, nothing to show");
  const picture = { id: "shared" } as unknown as MediaStream;
  (devices.vision as { stream?: MediaStream | null }).stream = picture;
  assert.equal(host.visionStream(), picture);
  (devices.vision as { stream?: MediaStream | null }).stream = null;
  assert.equal(host.visionStream(), null);
});

test("a yes/no question goes to the Mac app's own alert, and only its confirming button counts as yes", async () => {
  const { bridge, host } = harness();
  const asked: string[] = [];
  (bridge as unknown as { confirm: (m: string, l: string) => Promise<unknown> }).confirm = async (m, l) => {
    asked.push(`${l}: ${m}`);
    return asked.length === 1 ? true : "yes";
  };
  assert.equal(await host.confirm("Erase your GNSIS?", "Erase"), true);
  assert.equal(await host.confirm("Erase your GNSIS?", "Erase"), false, "anything but true is a no");
  assert.deepEqual(asked, ["Erase: Erase your GNSIS?", "Erase: Erase your GNSIS?"]);
});

test("the menu bar icon: main's clicks become tuck-away and come-back events, and its place is kept", async () => {
  const plain = harness();
  assert.equal(plain.host.capabilities().menuBar, false, "no icon, no tucking away");
  const { bridge, events, host } = harness({ menuBar: true });
  await tick();
  assert.equal(host.capabilities().menuBar, true);
  assert.equal(host.menuBarIcon(), null);
  bridge.menubar({ at: { x: 1390, y: -16 } });
  assert.deepEqual(host.menuBarIcon(), { x: 1390, y: -16 });
  assert.equal(events.filter((e) => e.type === "menubar").length, 0, "knowing where the icon is asks for nothing");
  bridge.menubar({ want: "hide", at: { x: 1392, y: -16 } });
  bridge.menubar({ want: "show" });
  assert.deepEqual(events.filter((e) => e.type === "menubar"), [{ type: "menubar", want: "hide" }, { type: "menubar", want: "show" }]);
  assert.deepEqual(host.menuBarIcon(), { x: 1392, y: -16 });
  bridge.menubar({ want: "explode", at: { x: "1", y: Number.NaN } });
  bridge.menubar(null);
  assert.equal(events.filter((e) => e.type === "menubar").length, 2, "nothing else is taken as a click");
  assert.deepEqual(host.menuBarIcon(), { x: 1392, y: -16 });
  host.hideToMenuBar();
  assert.equal(bridge.hides, 1);
});
