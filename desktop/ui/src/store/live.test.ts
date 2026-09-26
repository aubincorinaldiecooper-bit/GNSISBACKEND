import assert from "node:assert/strict";
import { test } from "node:test";
import { applyLiveEvent, barHeights, cutText, endLiveTurns, liveInfo, openTurns, startLiveState, type LiveState } from "./live";

const T0 = 1_000_000;
const fresh = (): LiveState => startLiveState("gnsis", T0, "ready");

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
  assert.equal(liveInfo(live, "GNSIS", T0 + 3000).status, "Connecting…");
  live = applyLiveEvent(live, { type: "link", state: "ready" }, T0).live;
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

test("the armed button's bars follow the level and stay flat when nobody speaks", () => {
  assert.deepEqual(barHeights(0, false, 0), [5, 5, 8, 5, 5]);
  const loud = barHeights(1, true, 0);
  assert.ok(loud.every((h) => h === 22), `all bars at the top: ${loud}`);
  const soft = barHeights(0.2, true, 3);
  assert.ok(soft.every((h) => h >= 6 && h <= 22), `bars in range: ${soft}`);
});
