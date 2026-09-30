import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { HOME, greetedBefore, keepOnScreen, loadPlace, markGreeted, savePlace } from "./place";

/** The page's own storage, as the app has it; `broken` behaves like storage that is switched off. */
function withStorage(opts: { broken?: boolean } = {}) {
  const items = new Map<string, string>();
  const check = () => {
    if (opts.broken) throw new Error("storage is unavailable");
  };
  (globalThis as { localStorage?: unknown }).localStorage = {
    getItem: (k: string) => (check(), items.get(k) ?? null),
    setItem: (k: string, v: string) => (check(), void items.set(k, String(v))),
    removeItem: (k: string) => void items.delete(k),
  };
  return items;
}

afterEach(() => {
  delete (globalThis as { localStorage?: unknown }).localStorage;
});

// The dock where it sits by default on a 1440 × 900 screen.
const dock = { left: 630, top: 807, right: 810, bottom: 874 };

test("GNSIS goes where it is dragged, and stops at the screen's edges", () => {
  assert.deepEqual(keepOnScreen({ dx: -300, dy: -400 }, dock, 1440, 900), { dx: -300, dy: -400 }, "anywhere on screen is fine");
  assert.deepEqual(keepOnScreen({ dx: -2000, dy: 0 }, dock, 1440, 900), { dx: 8 - 630, dy: 0 }, "left edge");
  assert.deepEqual(keepOnScreen({ dx: 2000, dy: 0 }, dock, 1440, 900), { dx: 1440 - 8 - 810, dy: 0 }, "right edge");
  assert.deepEqual(keepOnScreen({ dx: 0, dy: -2000 }, dock, 1440, 900), { dx: 0, dy: 8 - 807 }, "top edge");
  assert.deepEqual(keepOnScreen({ dx: 0, dy: 200 }, dock, 1440, 900), { dx: 0, dy: 900 - 8 - 874 }, "bottom edge");
});

test("when the chat opens above GNSIS near the top of the screen, everything moves down just enough to fit", () => {
  const high = { dx: 0, dy: 8 - 807 }; // the dock dragged to the top
  const withChat = { ...dock, top: 380 }; // the chat opened above it
  assert.deepEqual(keepOnScreen(high, withChat, 1440, 900), { dx: 0, dy: 8 - 380 });
  assert.deepEqual(keepOnScreen(high, dock, 1440, 900), high, "and it is back where it was put once the chat closes");
});

test("something too big for the screen stays where it sits by default on that side", () => {
  assert.deepEqual(keepOnScreen({ dx: 120, dy: -50 }, { left: -20, top: 100, right: 1500, bottom: 874 }, 1440, 900), { dx: 0, dy: -50 });
});

test("where the person left GNSIS is remembered, in whole pixels", () => {
  const items = withStorage();
  assert.deepEqual(loadPlace(), HOME, "the first launch starts at home");
  savePlace({ dx: -412.6, dy: -93.2 });
  assert.equal(items.get("gnsis.place"), '{"dx":-413,"dy":-93}');
  assert.deepEqual(loadPlace(), { dx: -413, dy: -93 });
});

test("nothing odd in storage moves GNSIS: it starts at home", () => {
  const items = withStorage();
  for (const bad of ["not json", "null", "[]", '{"dx":"left","dy":0}', '{"dx":1}', '{"dx":null,"dy":null}']) {
    items.set("gnsis.place", bad);
    assert.deepEqual(loadPlace(), HOME, bad);
  }
});

test("the greeting is remembered as shown", () => {
  const items = withStorage();
  assert.equal(greetedBefore(), false);
  markGreeted();
  assert.equal(items.get("gnsis.greeted"), "1");
  assert.equal(greetedBefore(), true);
});

test("without storage, GNSIS starts at home and still moves and greets; nothing fails", () => {
  for (const setUp of [() => undefined, () => withStorage({ broken: true })]) {
    delete (globalThis as { localStorage?: unknown }).localStorage;
    setUp();
    assert.deepEqual(loadPlace(), HOME);
    assert.doesNotThrow(() => savePlace({ dx: 10, dy: 10 }));
    assert.equal(greetedBefore(), false);
    assert.doesNotThrow(() => markGreeted());
  }
});
