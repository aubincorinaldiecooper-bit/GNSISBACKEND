import assert from "node:assert/strict";
import { test } from "node:test";
import * as React from "react";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { Identity, IdentityStore } from "../host";
import { SimulatedLiveHost } from "../hosts/simulated";
import { actions, configure, enterDesktop, resetStore } from "../store/store";
import { ChatWindow } from "./ChatWindow";
import { AgentPanel } from "./AgentPanel";

// The test runner compiles JSX the classic way, which looks for a global React.
(globalThis as { React?: typeof React }).React = React;

const identity: Identity = { publicId: "gnsis:TEST-TEST-TEST", publicKey: "", storage: "local" };
const ids: IdentityStore = { storageNote: "", load: async () => identity, create: async () => identity, erase: async () => {} };

test("the chat opens alone, with an Activity button and no empty agents panel; the button opens and closes it", () => {
  resetStore();
  configure(new SimulatedLiveHost(), ids);
  enterDesktop(identity, false);
  actions.openAgent("gnsis");
  const chat = renderToStaticMarkup(createElement(ChatWindow, { height: "auto" }));
  assert.match(chat, /aria-label="Show activity"/);
  assert.doesNotMatch(chat, /Your agents/);
  assert.doesNotMatch(chat, /New chat/, "no button that promises a new chat and reopens the old one");
  actions.toggleActivity();
  assert.match(renderToStaticMarkup(createElement(ChatWindow, { height: "auto" })), /aria-label="Hide activity"/);
  const drawer = renderToStaticMarkup(createElement(AgentPanel, { left: 0, width: 520, height: 600 }));
  assert.match(drawer, /Your agents/);
  assert.match(drawer, /No agents are running\./);
});

test("opening the Activity drawer never moves or resizes the chat, and the two never overlap", async () => {
  const { stageLayout, CHAT_W, DRAWER_MIN } = await import("./Stage");
  for (const w of [1100, 1180, 1280, 1366, 1440, 1512, 1728, 1920, 2560]) {
    const l = stageLayout(w, 900);
    // One layout for both states: the chat's place depends on the window only.
    assert.ok(l.chatW <= CHAT_W && l.chatW >= 560, `the chat keeps a readable width at ${w}px`);
    assert.ok(l.chatLeft >= 24);
    assert.ok(l.drawerLeft >= l.chatLeft + l.chatW + 24, `the drawer sits beside the chat, not over it, at ${w}px`);
    assert.ok(l.drawerLeft + l.drawerW <= w - 24, `the drawer fits the window at ${w}px`);
    assert.ok(l.drawerW >= DRAWER_MIN && l.drawerW <= 600, `the drawer is wide enough to read at ${w}px`);
  }
  // With room to spare, the chat is centred and full width.
  const wide = stageLayout(1920, 1080);
  assert.equal(wide.chatW, CHAT_W);
  assert.equal(wide.chatLeft, (1920 - CHAT_W) / 2);
});
