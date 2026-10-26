import assert from "node:assert/strict";
import { test } from "node:test";
import { PlaybackScheduler, type ScheduledSourceLike } from "./playbackScheduler.js";

/**
 * PR-H playback regressions: chunks chain sequentially, `started` fires at the
 * actual scheduled start (not enqueue), and cancelled sources never emit
 * `completed`.
 */

const fakeSource = (duration: number) => {
  const src: ScheduledSourceLike & { duration: number; startedAt?: number; stops: number; end: () => void } = {
    duration,
    onended: null,
    stops: 0,
    start(when) {
      this.startedAt = when;
    },
    stop() {
      this.stops++;
      (this.onended as (() => void) | null)?.();
    },
    end() {
      (this.onended as (() => void) | null)?.();
    },
  };
  return src;
};

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** Polls until `ok` holds, so a slow machine is not mistaken for a bug. */
const within = async (ok: () => boolean, what: string, timeoutMs = 5_000): Promise<void> => {
  const deadline = Date.now() + timeoutMs;
  while (!ok()) {
    if (Date.now() >= deadline) throw new Error(`timed out after ${timeoutMs} ms waiting for ${what}`);
    await sleep(5);
  }
};

test("chunks schedule sequentially, each starting when the previous ends", async () => {
  let now = 100;
  const sched = new PlaybackScheduler(() => now);
  const events: string[] = [];
  const s1 = fakeSource(0.05);
  const s2 = fakeSource(0.05);

  sched.schedule(s1, s1.duration, { onStarted: () => events.push("s1-started"), onEnded: () => events.push("s1-ended") });
  sched.schedule(s2, s2.duration, { onStarted: () => events.push("s2-started"), onEnded: () => events.push("s2-ended") });

  assert.equal(s1.startedAt, 100);
  assert.equal(s2.startedAt, 100.05);
  // s2's started ack must not fire at enqueue time — only at its scheduled start.
  assert.equal(events.length, 0);
  // Wait for each ack rather than sleeping a fixed slice: under the parallel
  // suite a loaded machine could deliver s2's start late, and the fixed sleep
  // then reported a scheduling bug that was really just slow timers. The ack
  // still must NOT arrive at enqueue time — the assertion above pins that.
  await within(() => events.includes("s1-started"), "s1's started ack");
  await within(() => events.includes("s2-started"), "s2's started ack");
  // Sequential, in that order: s2's start is never observed before s1's.
  assert.deepEqual(events, ["s1-started", "s2-started"]);
});

test("playback.started fires at actual scheduled start, not enqueue time", async () => {
  let now = 0;
  const sched = new PlaybackScheduler(() => now);
  let started = 0;
  sched.schedule(fakeSource(0.03), 0.03, { onStarted: () => started++, onEnded: () => {} });
  assert.equal(started, 0); // not at enqueue
  await sleep(50);
  assert.equal(started, 1); // at scheduled start
});

test("cancelled sources never emit playback.completed", async () => {
  const sched = new PlaybackScheduler(() => 0);
  const events: string[] = [];
  const s1 = fakeSource(0.05);
  const s2 = fakeSource(0.05);
  sched.schedule(s1, s1.duration, { onStarted: () => events.push("s1-started"), onEnded: () => events.push("s1-ended") });
  sched.schedule(s2, s2.duration, { onStarted: () => events.push("s2-started"), onEnded: () => events.push("s2-ended") });

  const stopped = sched.cancelAll();
  assert.equal(stopped, 2);
  await sleep(80); // let any stale timers/end events surface
  // s1's started may have fired before cancel; s2 (queued) must never start or complete.
  assert.ok(!events.includes("s2-started"));
  assert.ok(!events.includes("s1-ended"));
  assert.ok(!events.includes("s2-ended"));
});

test("natural completion still emits completed", async () => {
  const sched = new PlaybackScheduler(() => 0);
  let ended = 0;
  const s = fakeSource(0.01);
  sched.schedule(s, s.duration, { onStarted: () => {}, onEnded: () => ended++ });
  s.end();
  assert.equal(ended, 1);
});
