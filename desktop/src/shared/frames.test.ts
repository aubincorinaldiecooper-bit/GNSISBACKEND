import assert from "node:assert/strict";
import { test } from "node:test";
import {
  FRAME_FIT_PX,
  RECONNECT_BUDGET_MS,
  RECONNECT_DELAYS_MS,
  fitWithin,
  reconnectDelayMs,
  resolveFrameRate,
} from "./frames.js";

test("fitWithin scales the largest side to the model's fit, never upscales", () => {
  assert.deepEqual(fitWithin(1920, 1080), { width: 448, height: 252 });
  assert.deepEqual(fitWithin(320, 240), { width: 320, height: 240 });
  assert.deepEqual(fitWithin(896, 448), { width: FRAME_FIT_PX, height: 224 });
});

test("fitWithin respects a custom fit and never returns zero", () => {
  assert.deepEqual(fitWithin(1600, 900, 800), { width: 800, height: 450 });
  const tiny = fitWithin(1, 1);
  assert.ok(tiny.width >= 1 && tiny.height >= 1);
});

test("resolveFrameRate honors the daemon recommendation with guardrails", () => {
  assert.equal(resolveFrameRate(3.2), 3.2);
  assert.equal(resolveFrameRate(0), 1);
  assert.equal(resolveFrameRate(-5), 1);
  assert.equal(resolveFrameRate(undefined), 1);
  assert.equal(resolveFrameRate("bogus"), 1);
  assert.equal(resolveFrameRate(0, 2.5), 2.5);
});

test("reconnectDelayMs mirrors video.js backoff and budget", () => {
  const t0 = 1_000_000;
  assert.equal(reconnectDelayMs(0, t0, t0), RECONNECT_DELAYS_MS[0]);
  assert.equal(reconnectDelayMs(1, t0, t0), RECONNECT_DELAYS_MS[1]);
  assert.equal(
    reconnectDelayMs(50, t0, t0),
    RECONNECT_DELAYS_MS[RECONNECT_DELAYS_MS.length - 1],
  );
  assert.equal(reconnectDelayMs(0, t0, t0 + RECONNECT_BUDGET_MS + 1), null);
  assert.equal(reconnectDelayMs(-1, t0, t0), null);
});
