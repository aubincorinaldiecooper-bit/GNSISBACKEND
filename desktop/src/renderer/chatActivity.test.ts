import assert from "node:assert/strict";
import { test } from "node:test";
import * as React from "react";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { Identity, IdentityStore } from "@gnsis/ui";
import { actions, configure, enterDesktop, getState, hasConversation, resetStore, setState } from "../../ui/src/store/store";
import { ChatWindow } from "../../ui/src/components/ChatWindow";
import type { ActionUpdate, GnsisBridge, TurnResult } from "./bridge.js";
import { ElectronLiveHost, type Devices } from "./electronHost.js";

/**
 * The chat's "Thinking" and steps, driven the way the Mac app drives them:
 * the real Electron host, fed by a fake preload bridge (the runtime's controls
 * and audio, the main process's action updates) and fake devices (microphone
 * levels, playback), into the real store, rendered by the real chat.
 */

// The test runner compiles the UI's JSX the classic way, which looks for a global React.
(globalThis as { React?: typeof React }).React = React;

const identity: Identity = { publicId: "gnsis:TEST-TEST-TEST", publicKey: "", storage: "local" };
const ids: IdentityStore = { storageNote: "", load: async () => identity, create: async () => identity, erase: async () => {} };
const tick = () => new Promise((r) => setTimeout(r, 0));

function setUp(opts: { typedTurns?: boolean; sendTurn?: (text: string) => Promise<TurnResult> } = {}) {
  const on: Record<string, ((...a: any[]) => void) | undefined> = {};
  const bridge = {
    overlay: false,
    linkState: async () => ({ ready: { type: "ready", session_id: "s1", typed_turns: opts.typedTurns === true }, connected: true, closed: false }),
    sendTurn: opts.sendTurn ?? (async () => ({ ok: true, turnId: "typed-1" })),
    mediaPermissions: async () => ({}),
    sendHostEvent: () => {},
    sendControl: () => {},
    startCall: () => {},
    endCall: () => {},
    reconnect: () => {},
    onControl: (fn: (c: unknown) => void) => { on.control = fn; },
    onAudio: (fn: (pcm: Uint8Array) => void) => { on.audio = fn; },
    onClosed: (fn: () => void) => { on.closed = fn; },
    onScreen: (fn: () => void) => { on.screen = fn; },
    onInterrupted: (fn: () => void) => { on.interrupted = fn; },
    onAction: (fn: (u: ActionUpdate) => void) => { on.action = fn; },
  } as unknown as GnsisBridge;
  const playback = {
    speaking: false,
    onSpeaking: undefined as ((speaking: boolean) => void) | undefined,
    level: () => 0.4,
    play() { playback.speaking = true; playback.onSpeaking?.(true); },
    cancel() { const n = playback.speaking ? 1 : 0; playback.speaking = false; playback.onSpeaking?.(false); return n; },
  };
  const mic = { active: false, onLevel: undefined as ((level: number) => void) | undefined, async start() { mic.active = true; }, stop() { mic.active = false; } };
  const vision = { active: false, current: null, onAccepted: undefined, onEnded: undefined, applyChannel() {}, handleScreenControl() {}, async share() {}, stop() {} };
  let now = 50_000;
  const host = new ElectronLiveHost(bridge, { mic, playback, vision } as unknown as Devices, () => {}, {
    now: () => now,
    speechThreshold: 0.2,
    speechHangoverMs: 300,
    setInterval: (() => 0) as unknown as typeof setInterval,
    clearInterval: (() => {}) as unknown as typeof clearInterval,
  });
  host.attach();
  let generation = 0;
  return {
    host,
    /** A control message from the runtime, as the main process forwards it. */
    runtime: (c: unknown) => on.control?.(c),
    /** What the main process reports about an action it is carrying out. */
    action: (u: ActionUpdate) => on.action?.(u),
    /** Microphone energy, `ms` after the last reading. */
    level: (l: number, ms = 50) => { now += ms; mic.onLevel?.(l); },
    /** A reply's audio arrives and plays. */
    speak: () => { on.control?.({ type: "audio.chunk", generation_id: ++generation }); on.audio?.(new Uint8Array(480)); },
    /** The reply has finished playing. */
    finishSpeaking: () => { playback.speaking = false; playback.onSpeaking?.(false); },
    generation: () => generation,
    chat: () => renderToStaticMarkup(createElement(ChatWindow, { height: "auto" })),
  };
}

async function live() {
  resetStore();
  const app = setUp();
  configure(app.host, ids);
  enterDesktop(identity, false);
  await tick();
  actions.startLive("gnsis");
  await tick();
  await tick();
  assert.equal(getState().live?.phase, "listening", "the microphone is on");
  return app;
}

/** The person says something, then is quiet for longer than a pause. */
function speakAndStop(app: ReturnType<typeof setUp>) {
  app.level(0.6);
  app.level(0.7);
  app.level(0.05);
  app.level(0.05, 400);
}

test("from the Mac app's own events: Thinking once the person stops, the step while it runs, then one line once GNSIS answers", async () => {
  const app = await live();
  assert.equal(hasConversation(getState(), "gnsis"), false, "no chat window before anything is said");

  speakAndStop(app);
  let html = app.chat();
  assert.match(html, /class="thinking-row"/);
  assert.equal(hasConversation(getState(), "gnsis"), true, "the chat window opens to show GNSIS thinking");

  // GNSIS says what it will do, then the main process starts the step.
  app.runtime({ type: "chunk", text: "Sure, opening YouTube in Chrome.", end_of_turn: false, interrupted: false });
  assert.doesNotMatch(app.chat(), /thinking-row/, "its first words end Thinking");
  app.action({ callId: "c1", state: "working", text: "Open youtube.com in a new tab in Google Chrome" });
  html = app.chat();
  assert.match(html, /class="step-row is-running"/);
  assert.ok(
    html.indexOf("Sure, opening YouTube in Chrome.") < html.indexOf("Open youtube.com in a new tab in Google Chrome"),
    "the running step sits below the words said before it",
  );

  app.action({ callId: "c1", state: "done", text: "Google Chrome opened a new tab at youtube.com." });
  html = app.chat();
  assert.match(html, /class="step-row is-done"/);
  assert.match(html, />Google Chrome opened a new tab at youtube.com.</, "the tick says it is done");
  assert.doesNotMatch(html, /Done: Google Chrome/, "so the word does not repeat it");
  assert.doesNotMatch(html, /is-running/);

  // GNSIS answers: its steps fold into one line that opens them.
  app.runtime({ type: "chunk", text: "The pasta recipes are on screen.", end_of_turn: true, interrupted: false });
  html = app.chat();
  assert.match(html, /class="steps-summary" aria-expanded="false">Worked for \d+s/);
  assert.doesNotMatch(html, /step-row/);
  const [before, summary, answer] = ["Sure, opening YouTube in Chrome.", "Worked for", "The pasta recipes are on screen."].map((t) => html.indexOf(t));
  assert.ok(before < summary && summary < answer, "words, then the steps, then the answer");
  const kept = getState().convs.gnsis.turns.filter((t) => !t.greeting && t.role !== "user");
  assert.deepEqual(
    kept.map((t) => (t.step ? `step:${t.step.state}` : t.text)),
    ["Sure, opening YouTube in Chrome.", "step:done", "The pasta recipes are on screen."],
    "and the conversation keeps that order",
  );
  await app.host.endLive();
});

test("from the Mac app's own events: no Thinking over GNSIS's voice; a cut-off reply does think; a step that failed stays open", async () => {
  const app = await live();

  // GNSIS is speaking; the person talks over it and GNSIS carries on.
  app.speak();
  assert.equal(getState().live?.agentSpeaking, true);
  speakAndStop(app);
  assert.doesNotMatch(app.chat(), /thinking-row/, "not while GNSIS is still speaking");
  app.finishSpeaking();
  assert.doesNotMatch(app.chat(), /thinking-row/, "nor once it finishes, for words said over it");

  // The person cuts GNSIS off, then finishes: GNSIS is thinking about what they said.
  app.speak();
  app.level(0.6);
  app.runtime({ type: "playback.cancel", cancelled_generation_id: app.generation() });
  assert.equal(getState().live?.agentSpeaking, false, "the cut stops the reply at once");
  app.level(0.05);
  app.level(0.05, 400);
  assert.match(app.chat(), /class="thinking-row"/);

  // A step that fails: marked, and the list stays open under the answer.
  app.action({ callId: "c2", state: "working", text: "Move “report.pdf” into “Projects”" });
  app.action({ callId: "c2", state: "failed", text: "There is no folder called Projects." });
  app.runtime({ type: "chunk", text: "I couldn’t find that folder.", end_of_turn: true, interrupted: false });
  const html = app.chat();
  assert.match(html, /class="step-row is-failed"/);
  assert.match(html, /Couldn’t do it: There is no folder called Projects\./);
  assert.doesNotMatch(html, /steps-summary/, "a failed step is never folded away");
  await app.host.endLive();
});

test("from the Mac app's own events, a typed message: Sending… until the runtime has it, then Thinking between GNSIS's steps", async () => {
  resetStore();
  let accept!: () => void;
  const accepted = new Promise<void>((resolve) => (accept = resolve));
  const app = setUp({ typedTurns: true, sendTurn: async () => { await accepted; return { ok: true, turnId: "typed-1" }; } });
  configure(app.host, ids);
  enterDesktop(identity, false);
  await tick();
  assert.equal(getState().caps.text, true, "the runtime said it answers typed turns");

  setState({ text: "Open YouTube and search for pasta recipes", mode: "bar", winOpen: true, active: "gnsis" });
  actions.send();
  let html = app.chat();
  assert.match(html, /Open YouTube and search for pasta recipes/);
  assert.match(html, /Sending…/);
  assert.doesNotMatch(html, /thinking-row/);
  assert.equal(hasConversation(getState(), "gnsis"), true, "a typed message opens the chat");

  accept();
  await tick();
  await tick();
  html = app.chat();
  assert.match(html, /class="thinking-row"/, "the runtime has it: GNSIS is thinking");
  assert.doesNotMatch(html, /Sending…/);

  app.action({ callId: "c1", state: "working", text: "Open youtube.com in a new tab in Google Chrome" });
  html = app.chat();
  assert.match(html, /class="step-row is-running"/);
  assert.doesNotMatch(html, /thinking-row/, "the step shows instead");
  app.action({ callId: "c1", state: "done", text: "Google Chrome opened a new tab at youtube.com." });
  assert.match(app.chat(), /class="thinking-row"/, "still no answer: thinking again, below the step");

  app.runtime({ type: "chunk", text: "The pasta recipes are on screen.", end_of_turn: true, interrupted: false });
  html = app.chat();
  assert.doesNotMatch(html, /thinking-row/);
  assert.match(html, /class="steps-summary" aria-expanded="false">Worked for \d+s/);
  assert.equal(getState().awaiting, null, "the answer closes the message");
});
