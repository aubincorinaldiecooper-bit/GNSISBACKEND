import assert from "node:assert/strict";
import { test } from "node:test";
import { applyWindowRules, type RuledWindow } from "./windowRules.js";

function fakeWindow() {
  const calls: string[] = [];
  const win: RuledWindow = {
    setContentProtection: (enable) => void calls.push(`protect ${enable}`),
    setVisibleOnAllWorkspaces: (visible, options) => void calls.push(`everywhere ${visible} fullscreen ${options?.visibleOnFullScreen}`),
  };
  return { win, calls };
}

test("every GNSIS window is left out of screenshots and screen sharing", () => {
  for (const floating of [true, false]) {
    const { win, calls } = fakeWindow();
    applyWindowRules(win, floating);
    assert.ok(calls.includes("protect true"), `floating=${floating}`);
  }
});

test("the floating window follows the person to every desktop and over full-screen apps", () => {
  const { win, calls } = fakeWindow();
  assert.deepEqual(applyWindowRules(win, true), ["hidden from screen capture", "on every desktop and over full-screen apps"]);
  assert.ok(calls.includes("everywhere true fullscreen true"));
});

test("an ordinary window stays an ordinary window on its own desktop", () => {
  const { win, calls } = fakeWindow();
  assert.deepEqual(applyWindowRules(win, false), ["hidden from screen capture"]);
  assert.ok(!calls.some((c) => c.startsWith("everywhere")));
});
