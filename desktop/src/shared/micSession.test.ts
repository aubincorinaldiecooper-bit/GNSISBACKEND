import assert from "node:assert/strict";
import { test } from "node:test";
import { MicSession, type MicCaptureResources } from "./micSession.js";

/**
 * PR-H mute regression: mic toggling is mute semantics — it releases local
 * capture resources only, never touches the session control channel, and
 * repeated mute/unmute cycles cleanly reacquire.
 */

const fakeCapture = () => {
  const stops = { track: 0, ctx: 0 };
  const res: MicCaptureResources = {
    stream: { getTracks: () => [{ stop: () => stops.track++ }] },
    ctx: { close: async () => void stops.ctx++ },
  };
  return { res, stops };
};

test("mute releases tracks and audio context", async () => {
  const mic = new MicSession();
  const { res, stops } = fakeCapture();
  await mic.start(async () => res);
  assert.equal(mic.active, true);
  mic.stop();
  assert.equal(stops.track, 1);
  assert.equal(stops.ctx, 1);
  assert.equal(mic.active, false);
});

test("repeated mute/unmute reacquires cleanly without double-release", async () => {
  const mic = new MicSession();
  for (let i = 0; i < 3; i++) {
    const { res, stops } = fakeCapture();
    await mic.start(async () => res);
    assert.equal(mic.active, true);
    mic.stop();
    assert.equal(stops.track, 1, `cycle ${i}: each acquisition released once`);
    assert.equal(stops.ctx, 1);
  }
  // stop with nothing active is a no-op.
  mic.stop();
  assert.equal(mic.active, false);
});

test("start while active releases the previous capture first", async () => {
  const mic = new MicSession();
  const first = fakeCapture();
  const second = fakeCapture();
  await mic.start(async () => first.res);
  await mic.start(async () => second.res);
  assert.equal(first.stops.track, 1);
  assert.equal(first.stops.ctx, 1);
  assert.equal(second.stops.track, 0);
  mic.stop();
  assert.equal(second.stops.track, 1);
});
