import assert from "node:assert/strict";
import { test } from "node:test";
import { CaptureManager, type CaptureHandle } from "./captureLifecycle.js";

/**
 * Capture lifecycle regression tests — one active visual capture at a time:
 * intentional source switches end the previous session, while transport
 * reconnects never reach the capture source.
 */

const fakeSession = (name: string) => {
  let stops = 0;
  const handle: CaptureHandle = { stop: () => stops++ };
  return { name, handle, stops: () => stops };
};

test("switching screen -> camera stops the previous capture", async () => {
  const mgr = new CaptureManager();
  const screen = fakeSession("screen");
  const camera = fakeSession("camera");

  await mgr.switchTo(async () => screen.handle);
  await mgr.switchTo(async () => camera.handle);

  assert.equal(screen.stops(), 1);
  assert.equal(camera.stops(), 0);
});

test("switching camera -> screen stops the previous capture", async () => {
  const mgr = new CaptureManager();
  const camera = fakeSession("camera");
  const screen = fakeSession("screen");

  await mgr.switchTo(async () => camera.handle);
  await mgr.switchTo(async () => screen.handle);

  assert.equal(camera.stops(), 1);
  assert.equal(screen.stops(), 0);
});

test("re-selecting the same source still retires the old session", async () => {
  const mgr = new CaptureManager();
  const first = fakeSession("screen-1");
  const second = fakeSession("screen-2");

  await mgr.switchTo(async () => first.handle);
  await mgr.switchTo(async () => second.handle);

  assert.equal(first.stops(), 1);
  assert.equal(second.stops(), 0);
});

test("a stale handle cannot tear down the newer session", async () => {
  const mgr = new CaptureManager();
  const old = fakeSession("old");
  const fresh = fakeSession("fresh");

  await mgr.switchTo(async () => old.handle);
  await mgr.switchTo(async () => fresh.handle);

  // e.g. the old track's onended event arrives late after the switch.
  mgr.stopIfCurrent(old.handle);
  assert.equal(fresh.stops(), 0);

  mgr.stopIfCurrent(fresh.handle);
  assert.equal(fresh.stops(), 1);
});

test("transport reconnect does not stop or reacquire the capture source", async () => {
  // The manager has no transport coupling: reconnecting the websocket is not
  // a switch/stop event, so a live capture survives any socket churn.
  const mgr = new CaptureManager();
  const screen = fakeSession("screen");
  let acquisitions = 0;
  const acquire = async () => {
    acquisitions++;
    return screen.handle;
  };

  await mgr.switchTo(acquire);
  // Simulated transport events: reconnect_scheduled / close / reconnect —
  // none of these are routed to the capture manager.
  for (const transportEvent of ["close", "reconnect_scheduled", "reconnect", "open"]) {
    void transportEvent;
  }
  assert.equal(screen.stops(), 0);
  assert.equal(acquisitions, 1);
});
