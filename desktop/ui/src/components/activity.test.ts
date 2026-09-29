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

test("the Activity panel shows what GNSIS sees: nothing made up, the real picture once it is shared", async () => {
  const { setState } = await import("../store/store");
  const { ScreenCard } = await import("./ScreenView");
  const render = () => renderToStaticMarkup(createElement(ScreenCard));
  resetStore();
  const host = new SimulatedLiveHost({ screen: true, camera: true });
  configure(host, ids);
  enterDesktop(identity, false);

  const off = render();
  assert.match(off, /GNSIS isn’t looking at your screen or camera\./);
  assert.match(off, /Share my screen/);
  assert.match(off, /Use camera/);
  assert.doesNotMatch(off, /<video/);

  setState({ vision: { source: "screen", state: "starting" } });
  const starting = render();
  assert.match(starting, /Connecting to your screen…/);
  assert.match(starting, />Cancel</);
  assert.doesNotMatch(starting, /<video/, "no picture before GNSIS has one");

  // On, and the host has the picture: it is shown, with a way to open it large.
  const picture = {} as MediaStream;
  host.visionStream = () => picture;
  setState({ vision: { source: "screen", state: "on" } });
  const on = render();
  assert.match(on, /<video/);
  assert.match(on, /aria-label="Open your screen, as shared with GNSIS"/);
  assert.match(on, /Stop sharing/);

  // On, but this host cannot hand over its picture: say so, never show a stand-in.
  host.visionStream = () => null;
  const blind = render();
  assert.doesNotMatch(blind, /<video/);
  assert.match(blind, /can’t show you the picture here/);

  setState({ vision: { source: "screen", state: "denied", detail: "macOS hasn’t allowed GNSIS to record your screen." } });
  const denied = render();
  assert.match(denied, /macOS hasn’t allowed GNSIS to record your screen\./);
  assert.match(denied, /Try again/);
});

test("the screen view is only offered where the host can share a screen or camera", () => {
  resetStore();
  configure(new SimulatedLiveHost(), ids);
  enterDesktop(identity, false);
  actions.openAgent("gnsis");
  actions.toggleActivity();
  const drawer = renderToStaticMarkup(createElement(AgentPanel, { left: 0, width: 440, height: 600 }));
  assert.doesNotMatch(drawer, /What GNSIS sees/);
});

test("on narrow windows the chat keeps a readable width and the drawer lies over it, instead of squeezing it", async () => {
  const { stageLayout, CHAT_MIN } = await import("./Stage");
  for (const w of [360, 480, 768, 900, 1024, 1071]) {
    const l = stageLayout(w, 800);
    assert.equal(l.drawerOver, true, `${w}px is too narrow for both side by side`);
    assert.ok(l.chatW >= Math.min(CHAT_MIN, w - 48), `the chat is not squeezed at ${w}px (got ${l.chatW})`);
    assert.ok(l.chatLeft >= 0 && l.chatLeft + l.chatW <= w, `the chat fits at ${w}px`);
    assert.ok(l.drawerLeft >= 0 && l.drawerLeft + l.drawerW <= w, `the drawer fits at ${w}px`);
  }
  for (const w of [1072, 1100, 1440]) assert.equal(stageLayout(w, 800).drawerOver, false, `side by side at ${w}px`);
});

test("the dock sits on the chat's centre line, so opening the bar grows it in place", async () => {
  const { stageLayout, dockLeft } = await import("./Stage");
  for (const w of [1100, 1440, 1920]) {
    const l = stageLayout(w, 900);
    const dockW = 300;
    const left = dockLeft(l, dockW, w);
    assert.equal(Math.round(left + dockW / 2), Math.round(l.chatLeft + l.chatW / 2), `same centre at ${w}px`);
  }
});
