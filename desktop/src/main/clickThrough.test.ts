import assert from "node:assert/strict";
import { test } from "node:test";
import { ClickThrough, hitRectsFrom, overCard } from "./clickThrough.js";

function fakeWindow(zoom = 1) {
  const calls: boolean[] = [];
  let destroyed = false;
  return {
    calls,
    destroy: () => (destroyed = true),
    win: {
      isDestroyed: () => destroyed,
      getBounds: () => ({ x: 0, y: 25, width: 1440, height: 875 }),
      setIgnoreMouseEvents: (ignore: boolean) => void calls.push(ignore),
      webContents: { getZoomFactor: () => zoom },
    },
  };
}

test("clicks fall through the see-through window except over GNSIS's own cards", () => {
  const w = fakeWindow();
  let pointer = { x: 10, y: 40 };
  const through = new ClickThrough(w.win, () => pointer);
  assert.deepEqual(w.calls, [true], "until the page reports its cards, everything falls through");
  // The chat card, in page pixels (the window starts 25 px down, under the menu bar).
  through.setRects([[360, 400, 720, 300]]);
  assert.equal(through.takingClicks, false);
  pointer = { x: 700, y: 25 + 500 };
  through.update();
  assert.equal(through.takingClicks, true, "over the card: GNSIS takes the click");
  through.update();
  assert.deepEqual(w.calls, [true, false], "no repeat calls while nothing changes");
  pointer = { x: 700, y: 25 + 399 };
  through.update();
  assert.equal(through.takingClicks, false, "just above the card: the app underneath gets it");
  // The card closes while the pointer is where it was.
  pointer = { x: 700, y: 25 + 500 };
  through.update();
  through.setRects([]);
  assert.equal(through.takingClicks, false);
});

test("a page drawn smaller than 100% has its cards where they are drawn", () => {
  assert.equal(overCard([[100, 100, 100, 100]], 95, 95, 0.9), true, "90% zoom: the card starts at 90 px");
  assert.equal(overCard([[100, 100, 100, 100]], 185, 185, 0.9), false, "and ends at 180 px");
});

test("only well-formed rectangles are believed", () => {
  assert.deepEqual(hitRectsFrom("everything"), []);
  assert.deepEqual(hitRectsFrom([[0, 0, 10, 10], [0, 0, -5, 10], [0, 0, 10], ["0", 0, 1, 1], [0, 0, Infinity, 1]]), [[0, 0, 10, 10]]);
  assert.equal(hitRectsFrom(Array.from({ length: 500 }, () => [0, 0, 1, 1])).length, 64);
});

test("a closed window stops the watching", () => {
  const w = fakeWindow();
  const through = new ClickThrough(w.win, () => ({ x: 0, y: 0 }));
  w.destroy();
  through.update();
  assert.deepEqual(w.calls, [true]);
});

test("while GNSIS sends its own clicks, every click goes through its window, cards included", () => {
  const w = fakeWindow();
  const pointer = { x: 700, y: 25 + 500 };
  const through = new ClickThrough(w.win, () => pointer);
  through.setRects([[360, 400, 720, 300]]);
  assert.equal(through.takingClicks, true, "the pointer rests on a card");
  through.suspend();
  assert.equal(through.takingClicks, false, "GNSIS's click goes to the app underneath");
  through.update();
  assert.equal(through.takingClicks, false, "the pointer check does not take it back mid-action");
  through.suspend();
  through.resume();
  assert.equal(through.takingClicks, false, "still suspended until the last action ends");
  through.resume();
  assert.equal(through.takingClicks, true, "afterwards the card takes clicks again");
});
