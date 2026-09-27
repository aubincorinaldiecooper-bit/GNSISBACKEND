import assert from "node:assert/strict";
import { test } from "node:test";
import { ScreenWatch } from "./screenWatch.js";

test("with the screen shared, the answer waits for a view taken after the action", async () => {
  let now = 10_000;
  const watch = new ScreenWatch(() => now);
  watch.noteFrame(9_900, "screen");
  const look = watch.lookAfter(10_000, 1_000);
  now = 10_400;
  watch.noteFrame(10_350, "screen");
  assert.equal(await look, "fresh");
});

test("a camera frame is not a view of the screen", async () => {
  let now = 10_000;
  const watch = new ScreenWatch(() => now);
  watch.noteFrame(9_990, "screen");
  const look = watch.lookAfter(10_000, 50);
  now = 10_010;
  watch.noteFrame(10_010, "camera");
  assert.equal(await look, "not_yet");
});

test("with no screen shared, the model is told it cannot check by looking", async () => {
  const watch = new ScreenWatch(() => 50_000);
  assert.equal(await watch.lookAfter(49_000), "not_shared");
  watch.noteFrame(10_000, "screen");
  // Frames stopped long ago: the share has ended.
  assert.equal(await new ScreenWatch(() => 50_000).lookAfter(49_000), "not_shared");
});
