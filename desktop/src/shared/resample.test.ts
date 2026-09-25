import assert from "node:assert/strict";
import { test } from "node:test";
import { Resampler } from "./resample.js";

/**
 * PR-H resampler regression: the fractional source position must carry across
 * blocks so sustained resampling holds the target rate (no ~16.19 kHz drift).
 */

test("44.1 kHz source sustains ~16 kHz output across blocks", () => {
  const r = new Resampler(44100 / 16000, 320);
  const block = new Float32Array(128).fill(0.5);
  let emitted = 0;
  // ~2 seconds of source audio at 44.1 kHz (128-sample blocks).
  const blocks = Math.ceil((44100 * 2) / 128);
  for (let i = 0; i < blocks; i++) {
    for (const chunk of r.push(block)) emitted += chunk.length;
  }
  const expected = 16000 * 2;
  assert.ok(
    Math.abs(emitted - expected) <= expected * 0.01,
    `emitted ${emitted} samples, expected ~${expected}`,
  );
});

test("48 kHz source emits exactly 16 kHz", () => {
  const r = new Resampler(48000 / 16000, 320);
  const block = new Float32Array(128).fill(1);
  let emitted = 0;
  for (let i = 0; i < 375; i++) {
    for (const chunk of r.push(block)) emitted += chunk.length;
  }
  assert.equal(emitted, 48000 / 3); // 48000 source samples -> 16000
});

test("chunk payloads are clipped PCM16", () => {
  const r = new Resampler(1, 4); // 1:1, tiny chunks
  const out = r.push(new Float32Array([2, -2, 0.5, -0.5]));
  assert.equal(out.length, 1);
  assert.deepEqual([...out[0]], [32767, -32767, 16384, -16383]);
});
