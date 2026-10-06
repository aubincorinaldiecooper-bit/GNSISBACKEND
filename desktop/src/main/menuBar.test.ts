import assert from "node:assert/strict";
import { test } from "node:test";
import { MenuBar, type MenuBarMessage } from "./menuBar.js";

/**
 * GNSIS's menu bar icon, with a fake tray, window and timers: when the icon
 * appears, what a click asks of the page, when the window hides and shows.
 */

const FACE = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACQAAAAk";

function setUp(opts: { closed?: boolean; menu?: unknown } = {}) {
  const sent: MenuBarMessage[] = [];
  const calls: string[] = [];
  const logs: string[] = [];
  const timers: Array<{ fn: () => void; ms: number; live: boolean }> = [];
  const trays: Array<{ images: unknown[]; tip: string; click?: () => void; rightClick?: () => void; popUps: unknown[]; destroyed: boolean }> = [];
  const win = {
    visible: true,
    hides: 0,
    shows: 0,
    isDestroyed: () => false,
    isVisible: () => win.visible,
    hide: () => { win.visible = false; win.hides++; },
    showInactive: () => { win.visible = true; win.shows++; },
    // The floating window covers the screen below a 25-point menu bar.
    getBounds: () => ({ x: 0, y: 25, width: 1440, height: 875 }),
  };
  const bar = new MenuBar({
    makeTray: (image) => {
      const t = {
        images: [image] as unknown[],
        tip: "",
        click: undefined as (() => void) | undefined,
        rightClick: undefined as (() => void) | undefined,
        popUps: [] as unknown[],
        destroyed: false,
        setImage: (i: unknown) => void t.images.push(i),
        setToolTip: (s: string) => void (t.tip = s),
        getBounds: () => ({ x: 1100, y: 0, width: 24, height: 24 }),
        on: (e: "click" | "right-click", fn: () => void) => void (e === "click" ? (t.click = fn) : (t.rightClick = fn)),
        popUpContextMenu: (menu?: unknown) => void t.popUps.push(menu),
        destroy: () => void (t.destroyed = true),
      };
      trays.push(t);
      return t;
    },
    image: (png) => `image of ${png.length} chars`,
    window: () => (opts.closed ? null : win),
    reopen: () => void calls.push("reopen"),
    endCall: () => void calls.push("endCall"),
    rightClickMenu: opts.menu === undefined ? undefined : () => opts.menu,
    send: (m) => void sent.push(m),
    zoom: 0.8,
    log: (l) => void logs.push(l),
    setTimeout: (fn, ms) => {
      const t = { fn, ms, live: true };
      timers.push(t);
      return t;
    },
    clearTimeout: (h) => void ((h as { live: boolean }).live = false),
  });
  return { bar, sent, logs, timers, trays, win, calls };
}

// The icon's centre (1112, 12) in page pixels from the window's top left, at the page's 80%.
const ICON = { x: 1390, y: -16 };

test("no icon until the page hands over GNSIS's face; then one icon, changed in place", () => {
  const { bar, sent, trays } = setUp();
  assert.equal(trays.length, 0);
  assert.equal(bar.iconAt(), null);
  bar.setFace(FACE);
  assert.equal(trays.length, 1);
  assert.equal(trays[0].tip, "GNSIS");
  assert.deepEqual(sent, [{ at: ICON }], "the page learns where the icon is");
  bar.setFace(FACE + "AAAA");
  assert.equal(trays.length, 1, "a new face changes the icon; it does not add one");
  assert.equal(trays[0].images.length, 2);
});

test("only a small PNG becomes the icon", () => {
  const { bar, trays, logs } = setUp();
  for (const bad of [42, null, "https://example.com/face.png", "data:image/svg+xml;base64,PHN2Zz4=", `data:image/png;base64,${"A".repeat(300_000)}`]) bar.setFace(bad);
  assert.equal(trays.length, 0);
  assert.equal(logs.filter((l) => l.includes("ignored")).length, 5);
});

test("clicking the icon asks the page to tuck GNSIS away; the window hides once it has", () => {
  const { bar, sent, timers, trays, win } = setUp();
  bar.setFace(FACE);
  trays[0].click!();
  assert.deepEqual(sent.at(-1), { want: "hide", at: ICON });
  assert.equal(win.visible, true, "not before GNSIS has shrunk into the icon");
  bar.pageTuckedAway();
  assert.equal(win.visible, false);
  assert.equal(timers[0].live, false, "the fallback is no longer needed");
  assert.equal(bar.tucked, true);
});

test("if the page never answers, the call is ended from here and the window hides anyway", () => {
  const { bar, calls, logs, timers, trays, win } = setUp();
  bar.setFace(FACE);
  trays[0].click!();
  assert.equal(timers[0].ms, 1_000);
  assert.deepEqual(calls, []);
  timers[0].fn();
  assert.deepEqual(calls, ["endCall"], "nothing keeps listening behind a hidden GNSIS");
  assert.equal(win.visible, false);
  assert.ok(logs.some((l) => l.includes("did not tuck away in time")));
});

test("if GNSIS's window was closed, the icon opens it again", () => {
  const { bar, calls, trays } = setUp({ closed: true });
  bar.setFace(FACE);
  trays[0].click!();
  assert.deepEqual(calls, ["reopen"]);
  bar.show();
  assert.deepEqual(calls, ["reopen", "reopen"], "and so does the Dock icon");
});

test("clicking the icon again brings GNSIS back; so does the Dock icon after the ≡ menu hid it", () => {
  const { bar, sent, trays, win } = setUp();
  bar.setFace(FACE);
  trays[0].click!();
  bar.pageTuckedAway();
  trays[0].click!();
  assert.equal(win.visible, true);
  assert.equal(win.shows, 1, "shown without taking the focus from the app in front");
  assert.deepEqual(sent.at(-1), { want: "show", at: ICON });
  assert.equal(bar.tucked, false);
  // "Hide to menu bar" in GNSIS's own menu: the page tucks away first, then says so.
  bar.pageTuckedAway();
  assert.equal(bar.tucked, true);
  bar.show();
  assert.equal(win.visible, true);
});

test("a second click while GNSIS is tucking away brings it straight back", () => {
  const { bar, sent, timers, trays, win } = setUp();
  bar.setFace(FACE);
  trays[0].click!();
  trays[0].click!();
  assert.deepEqual(sent.at(-1), { want: "show", at: ICON });
  assert.equal(timers[0].live, false);
  assert.equal(win.hides, 0, "the window never went");
});

test("quitting removes the icon", () => {
  const { bar, trays } = setUp();
  bar.setFace(FACE);
  bar.destroy();
  assert.equal(trays[0].destroyed, true);
  assert.equal(bar.tucked, false);
});

test("a right-click on the icon opens its small menu with Quit GNSIS; a click still tucks away", () => {
  const { bar, sent, trays } = setUp({ menu: "Quit GNSIS menu" });
  bar.setFace(FACE);
  trays[0].rightClick?.();
  assert.deepEqual(trays[0].popUps, ["Quit GNSIS menu"]);
  assert.deepEqual(sent, [{ at: ICON }], "the right-click asks nothing of the page");
  trays[0].click?.();
  assert.ok(sent.some((m: MenuBarMessage) => m.want === "hide"), "a normal click still asks the page to tuck away");
});

test("without a menu from main, a right-click opens nothing", () => {
  const { bar, trays } = setUp();
  bar.setFace(FACE);
  trays[0].rightClick?.();
  assert.deepEqual(trays[0].popUps, []);
});
