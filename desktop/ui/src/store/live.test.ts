import assert from "node:assert/strict";
import { test } from "node:test";
import type { LiveEvent } from "../host";
import type { Turn } from "../demo/data";
import { THINKING_MAX_MS, applyLiveEvent, barHeights, cutText, endLiveTurns, liveInfo, liveThinking, openTurns, startLiveState, type LiveState } from "./live";

const T0 = 1_000_000;
/** A session whose microphone came on at T0. */
const fresh = (): LiveState => applyLiveEvent(startLiveState("gnsis", T0, "ready"), { type: "mic", state: "on" }, T0).live;

test("the reply's words accumulate and become one turn when the runtime ends it", () => {
  let live = fresh();
  let step = applyLiveEvent(live, { type: "agent.text", text: "Hi there,", endOfTurn: false, interrupted: false }, T0 + 100);
  live = step.live;
  assert.equal(step.commit.length, 0);
  assert.deepEqual(openTurns(live), [{ role: "agent", text: "Hi there,", stream: 9, speaking: false }]);
  step = applyLiveEvent(live, { type: "agent.text", text: " how are you?", endOfTurn: true, interrupted: false }, T0 + 200);
  assert.deepEqual(step.commit, [{ role: "agent", text: "Hi there, how are you?", stream: 22 }]);
  assert.equal(step.live.agent, null);
});

test("an interrupted reply ends on a dash with the half word dropped", () => {
  assert.equal(cutText("Want me to walk you thr"), "Want me to walk you—");
  assert.equal(cutText("Word"), "Word—");
  assert.equal(cutText("Two words "), "Two words—");
  let live = fresh();
  live = applyLiveEvent(live, { type: "agent.text", text: "That makes sense. Want me to wal", endOfTurn: false, interrupted: false }, T0).live;
  const step = applyLiveEvent(live, { type: "agent.text", text: "", endOfTurn: false, interrupted: true }, T0 + 50);
  assert.deepEqual(step.commit, [{ role: "agent", text: "That makes sense. Want me to—", stream: 29 }]);
  assert.equal(step.live.agent, null);
});

test("cancelled playback cuts the open reply and clears the speaking state", () => {
  let live = fresh();
  live = applyLiveEvent(live, { type: "agent.speaking", speaking: true }, T0).live;
  live = applyLiveEvent(live, { type: "agent.level", level: 0.7 }, T0).live;
  live = applyLiveEvent(live, { type: "agent.text", text: "First one: ask what the price incl", endOfTurn: false, interrupted: false }, T0).live;
  const step = applyLiveEvent(live, { type: "agent.cut", reason: "daemon_cancel" }, T0 + 10);
  assert.deepEqual(step.commit, [{ role: "agent", text: "First one: ask what the price—", stream: 30 }]);
  assert.equal(step.live.agentSpeaking, false);
  assert.equal(step.live.agentLevel, 0);
  // Nothing open: a second cut commits nothing.
  assert.equal(applyLiveEvent(step.live, { type: "agent.cut", reason: "x" }, T0 + 20).commit.length, 0);
});

test("a spoken turn without words is kept as a spoken marker with its length, never invented text", () => {
  let live = fresh();
  live = applyLiveEvent(live, { type: "user.speech", state: "start" }, T0 + 1000).live;
  assert.deepEqual(openTurns(live), [{ role: "user", text: "", spoken: true, speaking: true }]);
  const step = applyLiveEvent(live, { type: "user.speech", state: "end" }, T0 + 4200);
  assert.deepEqual(step.commit, [{ role: "user", text: "", spoken: true, spokenMs: 3200 }]);
  assert.equal(step.live.user, null);
  // The host's own duration wins when it sends one.
  const withMs = applyLiveEvent(applyLiveEvent(fresh(), { type: "user.speech", state: "start" }, T0).live, { type: "user.speech", state: "end", ms: 900 }, T0 + 5000);
  assert.equal(withMs.commit[0].spokenMs, 900);
});

test("words from a transcribing host fill the spoken turn", () => {
  let live = fresh();
  live = applyLiveEvent(live, { type: "user.speech", state: "start" }, T0).live;
  live = applyLiveEvent(live, { type: "user.words", text: "Yes, let’s", final: false }, T0 + 500).live;
  assert.equal(live.user?.text, "Yes, let’s");
  live = applyLiveEvent(live, { type: "user.words", text: "Yes, let’s do that.", final: true }, T0 + 900).live;
  const step = applyLiveEvent(live, { type: "user.speech", state: "end", ms: 1000 }, T0 + 1000);
  assert.deepEqual(step.commit, [{ role: "user", text: "Yes, let’s do that.", spoken: true, spokenMs: 1000 }]);
  // A final transcript that arrives after the turn closed still lands.
  const late = applyLiveEvent(step.live, { type: "user.words", text: "Just the first part.", final: true }, T0 + 1200);
  assert.deepEqual(late.commit, [{ role: "user", text: "Just the first part.", spoken: true }]);
});

test("losing the link or the microphone ends the session with a reason", () => {
  const lost = applyLiveEvent(fresh(), { type: "link", state: "closed" }, T0);
  assert.equal(lost.ended?.reason, "The session ended.");
  const failed = applyLiveEvent(fresh(), { type: "link", state: "error", detail: "GNSIS could not be reached." }, T0);
  assert.equal(failed.ended?.reason, "GNSIS could not be reached.");
  const denied = applyLiveEvent(fresh(), { type: "mic", state: "denied", detail: "The microphone was not allowed." }, T0);
  assert.equal(denied.ended?.reason, "The microphone was not allowed.");
  assert.equal(applyLiveEvent(fresh(), { type: "mic", state: "on" }, T0).ended, undefined);
  assert.equal(applyLiveEvent(fresh(), { type: "link", state: "ready" }, T0).ended, undefined);
});

test("ending live closes what was open and writes the duration line", () => {
  let live = fresh();
  live = applyLiveEvent(live, { type: "agent.speaking", speaking: true }, T0).live;
  live = applyLiveEvent(live, { type: "agent.text", text: "Sure, starting with the fir", endOfTurn: false, interrupted: false }, T0).live;
  live = applyLiveEvent(live, { type: "user.speech", state: "start" }, T0 + 60_000).live;
  const turns = endLiveTurns(live, T0 + 65_000);
  assert.deepEqual(turns, [
    { role: "agent", text: "Sure, starting with the—", stream: 24 },
    { role: "user", text: "", spoken: true, spokenMs: 5000 },
    { role: "system", text: "Live conversation · 1:05" },
  ]);
  // A reply that was no longer playing is kept whole, and a note is appended when given.
  let quiet = fresh();
  quiet = applyLiveEvent(quiet, { type: "agent.text", text: "Done.", endOfTurn: false, interrupted: false }, T0).live;
  assert.deepEqual(endLiveTurns(quiet, T0 + 500, "The connection failed."), [
    { role: "agent", text: "Done.", stream: 5 },
    { role: "system", text: "Live conversation · 0:01" },
    { role: "system", text: "The connection failed." },
  ]);
});

test("the status line says what is happening, with the clock", () => {
  const off = liveInfo(null, "", T0);
  assert.equal(off.on, false);
  let live = startLiveState("gnsis", T0, "connecting");
  assert.equal(liveInfo(live, "GNSIS", T0 + 3000).status, "Connecting to GNSIS…");
  live = applyLiveEvent(live, { type: "link", state: "ready" }, T0).live;
  assert.equal(liveInfo(live, "GNSIS", T0 + 3000).status, "Connecting to GNSIS…", "a ready runtime is not a listening microphone");
  live = applyLiveEvent(live, { type: "mic", state: "on" }, T0).live;
  assert.equal(liveInfo(live, "GNSIS", T0 + 3000).status, "Listening · 0:03");
  live = applyLiveEvent(live, { type: "agent.speaking", speaking: true }, T0).live;
  live = applyLiveEvent(live, { type: "agent.level", level: 0.5 }, T0).live;
  const speaking = liveInfo(live, "GNSIS", T0 + 11_000);
  assert.equal(speaking.status, "GNSIS is speaking · 0:11");
  assert.equal(speaking.agentNow, true);
  assert.equal(speaking.amp, 0.5);
  live = applyLiveEvent(live, { type: "agent.speaking", speaking: false }, T0).live;
  live = { ...live, muted: true };
  assert.equal(liveInfo(live, "GNSIS", T0 + 65_000).status, "You’re muted · 1:05");
  // Muted: the microphone's level is ignored even if the host still sends one.
  assert.equal(applyLiveEvent(live, { type: "user.level", level: 0.9 }, T0).live.userLevel, 0);
});

test("the armed button's bars follow the level, stay short in silence, and never fill the button as one block", () => {
  assert.deepEqual(barHeights(0, false, 0), [5, 5, 8, 5, 5]);
  for (let t = 0; t < 40; t += 1) {
    const loud = barHeights(1, true, t);
    assert.ok(loud.every((h) => h >= 5 && h <= 18), `bars stay well inside the button: ${loud}`);
    assert.ok(new Set(loud).size > 1, `never one flat block: ${loud}`);
    assert.ok(loud[2] >= loud[0] && loud[2] >= loud[4], `tallest in the middle: ${loud}`);
  }
  const loud = barHeights(1, true, 3);
  const soft = barHeights(0.1, true, 3);
  assert.ok(soft.reduce((a, b) => a + b) < loud.reduce((a, b) => a + b), `a louder voice makes taller bars: ${soft} vs ${loud}`);
});

test("what GNSIS did on the computer lands in the chat as a plain line; the start of it does not", () => {
  const live = startLiveState("gnsis", T0, "ready");
  const said = (state: "working" | "waiting" | "done" | "failed" | "declined" | "needs_permission", text: string) =>
    applyLiveEvent(live, { type: "action", state, text }, T0).commit;
  assert.deepEqual(said("working", "Move “report.pdf” into “Projects”"), []);
  // Each line is a step: how it ended, and when.
  assert.deepEqual(said("waiting", "Waiting for your OK: Move “report.pdf” into “Projects”"), [
    { role: "system", text: "Waiting for your OK: Move “report.pdf” into “Projects”", step: { state: "waiting", endedAt: T0 } },
  ]);
  assert.deepEqual(said("done", "Moved report.pdf into ~/Documents/Projects."), [
    { role: "system", text: "Done: Moved report.pdf into ~/Documents/Projects.", step: { state: "done", endedAt: T0 } },
  ]);
  assert.deepEqual(said("failed", "No folder called Taxes."), [
    { role: "system", text: "Couldn’t do it: No folder called Taxes.", step: { state: "failed", endedAt: T0 } },
  ]);
  assert.deepEqual(said("declined", "Not done: Move “report.pdf” into “Projects”"), [
    { role: "system", text: "Not done: Move “report.pdf” into “Projects”", step: { state: "declined", endedAt: T0 } },
  ]);
});

test("pressing the voice button is not listening: only the microphone coming on is", () => {
  let live = startLiveState("gnsis", T0, "ready");
  assert.equal(live.phase, "connecting");
  let info = liveInfo(live, "GNSIS", T0 + 40_000);
  assert.equal(info.connecting, true);
  assert.equal(info.status, "Connecting to GNSIS…");
  // Nothing about the person or the reply counts while nothing is being captured.
  live = applyLiveEvent(live, { type: "agent.speaking", speaking: true }, T0).live;
  assert.equal(live.phase, "connecting");
  assert.equal(liveInfo(live, "GNSIS", T0).agentNow, false);
  live = applyLiveEvent(live, { type: "agent.speaking", speaking: false }, T0).live;
  // The microphone comes on 40 seconds later: listening, and the clock starts now.
  live = applyLiveEvent(live, { type: "mic", state: "on" }, T0 + 40_000).live;
  assert.equal(live.phase, "listening");
  info = liveInfo(live, "GNSIS", T0 + 45_000);
  assert.equal(info.connecting, false);
  assert.equal(info.status, "Listening · 0:05");
  // A reply actually playing makes it responding; its end, or a cut, makes it listen again.
  live = applyLiveEvent(live, { type: "agent.speaking", speaking: true }, T0 + 46_000).live;
  assert.equal(live.phase, "responding");
  live = applyLiveEvent(live, { type: "agent.cut", reason: "user_spoke" }, T0 + 47_000).live;
  assert.equal(live.phase, "listening");
});

test("a session ended while still connecting leaves no conversation behind, only the reason if there was one", () => {
  const live = startLiveState("gnsis", T0, "connecting");
  assert.deepEqual(endLiveTurns(live, T0 + 5000), [], "the person cancelled: nothing to record");
  assert.deepEqual(endLiveTurns(live, T0 + 5000, "Live voice couldn’t start. GNSIS could not be reached."), [
    { role: "system", text: "Live voice couldn’t start. GNSIS could not be reached." },
  ]);
});

test("Thinking: from the person's last word until GNSIS says or does anything; never over GNSIS speaking; never for long", () => {
  let live = fresh();
  const at = (e: LiveEvent, t: number) => (live = applyLiveEvent(live, e, t).live);
  const said = (from: number, to: number) => {
    at({ type: "user.speech", state: "start" }, from);
    assert.equal(liveThinking(live, from), false, "not while the person is speaking");
    at({ type: "user.speech", state: "end", ms: to - from }, to);
  };
  said(T0 + 100, T0 + 1000);
  assert.equal(liveThinking(live, T0 + 1000), true);
  assert.equal(liveThinking(live, T0 + 1000 + THINKING_MAX_MS - 1), true);
  assert.equal(liveThinking(live, T0 + 1000 + THINKING_MAX_MS), false, "silence is not shown as thinking for long");
  at({ type: "agent.text", text: "", endOfTurn: true, interrupted: false }, T0 + 1200);
  assert.equal(liveThinking(live, T0 + 1200), true, "an empty end of a reply is not an answer");
  at({ type: "agent.text", text: "Sure.", endOfTurn: false, interrupted: false }, T0 + 1500);
  assert.equal(liveThinking(live, T0 + 1500), false, "GNSIS's first words end it");
  at({ type: "agent.text", text: "", endOfTurn: true, interrupted: false }, T0 + 1600);

  said(T0 + 2000, T0 + 3000);
  at({ type: "action", state: "working", text: "Open Safari" }, T0 + 3100);
  assert.equal(liveThinking(live, T0 + 3100), false, "a step ends it");
  at({ type: "action", state: "done", text: "Safari is open." }, T0 + 3200);

  said(T0 + 4000, T0 + 5000);
  at({ type: "agent.speaking", speaking: true }, T0 + 5100);
  assert.equal(liveThinking(live, T0 + 5100), false, "GNSIS's voice ends it");

  // The person talks while GNSIS is still speaking, and GNSIS carries on:
  // nothing to think about, now or once the reply has finished.
  said(T0 + 6000, T0 + 6500);
  assert.equal(liveThinking(live, T0 + 6500), false);
  at({ type: "agent.speaking", speaking: false }, T0 + 7000);
  assert.equal(liveThinking(live, T0 + 7000), false);

  // The person cuts GNSIS off, then finishes: GNSIS is thinking about what they said.
  at({ type: "agent.speaking", speaking: true }, T0 + 8000);
  at({ type: "user.speech", state: "start" }, T0 + 8100);
  at({ type: "agent.cut", reason: "user_spoke" }, T0 + 8200);
  at({ type: "user.speech", state: "end", ms: 900 }, T0 + 9000);
  assert.equal(liveThinking(live, T0 + 9000), true);
});

test("a step lands below the words GNSIS said before it; words said while it runs stay below it", () => {
  let live = fresh();
  const turns: Turn[] = [];
  const at = (e: LiveEvent, t = T0) => {
    const step = applyLiveEvent(live, e, t);
    live = step.live;
    turns.push(...step.commit);
  };
  const order = () => turns.map((t) => (t.step ? `step:${t.step.state}` : t.text));
  const openWords = () => live.agent?.text ?? null;
  at({ type: "agent.text", text: "Sure, opening YouTube in Chrome.", endOfTurn: false, interrupted: false });
  at({ type: "action", state: "working", text: "Open youtube.com in a new tab in Google Chrome" });
  assert.deepEqual(order(), ["Sure, opening YouTube in Chrome."], "the words before the step close as it starts");
  assert.equal(openWords(), null);
  at({ type: "agent.text", text: "One moment", endOfTurn: false, interrupted: false });
  at({ type: "action", state: "done", text: "Google Chrome opened a new tab at youtube.com." });
  assert.equal(openWords(), "One moment", "words still arriving are not cut off by the step's end");
  at({ type: "agent.text", text: ", it’s open.", endOfTurn: true, interrupted: false });
  assert.deepEqual(order(), ["Sure, opening YouTube in Chrome.", "step:done", "One moment, it’s open."]);

  // Asking for the person's OK and then running is one step: the words are split once, where it began.
  at({ type: "agent.text", text: "I need your OK", endOfTurn: false, interrupted: false });
  at({ type: "action", state: "waiting", text: "Waiting for your OK: Press enter" });
  at({ type: "agent.text", text: " for this one.", endOfTurn: false, interrupted: false });
  at({ type: "action", state: "working", text: "Press enter" });
  at({ type: "action", state: "done", text: "Pressed enter." });
  at({ type: "agent.text", text: "", endOfTurn: true, interrupted: false });
  assert.deepEqual(order().slice(3), ["I need your OK", "step:waiting", "step:done", " for this one."]);
});
