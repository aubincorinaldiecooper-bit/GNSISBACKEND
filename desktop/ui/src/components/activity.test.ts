import assert from "node:assert/strict";
import { test } from "node:test";
import * as React from "react";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { Identity, IdentityStore } from "../host";
import { SimulatedLiveHost } from "../hosts/simulated";
import { actions, configure, enterDesktop, getState, hasConversation, resetStore, setState } from "../store/store";
import { ChatWindow } from "./ChatWindow";
import { AgentPanel } from "./AgentPanel";
import { DockMenu } from "./Overlays";

// The test runner compiles JSX the classic way, which looks for a global React.
(globalThis as { React?: typeof React }).React = React;

const identity: Identity = { publicId: "gnsis:TEST-TEST-TEST", publicKey: "", storage: "local" };
const ids: IdentityStore = { storageNote: "", load: async () => identity, create: async () => identity, erase: async () => {} };

test("the chat opens alone, with an Activity button; the button opens and closes the panel", () => {
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
  // The owner's call (30 September): no "GNSIS · Your assistant" heading, no
  // "GNSIS right now" card and no "Your agents" list in GNSIS's own panel.
  assert.match(drawer, /aria-label="Hide panel"/);
  assert.doesNotMatch(drawer, /Your assistant/);
  assert.doesNotMatch(drawer, /GNSIS right now/);
  assert.doesNotMatch(drawer, /Your agents/);
  assert.doesNotMatch(drawer, /No agents are running/);
  setState({ asking: { to: "gnsis", text: "Waiting for your OK: Press enter" } });
  assert.doesNotMatch(renderToStaticMarkup(createElement(AgentPanel, { left: 0, width: 520, height: 600 })), /Needs you/);
});

test("before there is a conversation there is no chat window, only the bar", () => {
  resetStore();
  configure(new SimulatedLiveHost({ text: false, transcript: false }), ids);
  enterDesktop(identity, false);
  actions.openAgent("gnsis");
  // GNSIS's opening line alone is not a conversation.
  assert.equal(hasConversation(getState(), "gnsis"), false);
  // Something said without words to show is not one either.
  const g = getState().convs.gnsis;
  setState({ convs: { ...getState().convs, gnsis: { ...g, turns: [...g.turns, { role: "user", text: "", spoken: true, spokenMs: 4000 }] } } });
  assert.equal(hasConversation(getState(), "gnsis"), false);
  // A typed message is.
  setState({ text: "Can you tidy up my desktop?" });
  actions.send();
  assert.equal(hasConversation(getState(), "gnsis"), true);
});

test("Thinking counts the seconds GNSIS has been at it, and says “Thinking some more” from five", () => {
  resetStore();
  configure(new SimulatedLiveHost({ text: false, transcript: false }), ids);
  enterDesktop(identity, false);
  actions.openAgent("gnsis");
  const g = getState().convs.gnsis;
  setState({ convs: { ...getState().convs, gnsis: { ...g, turns: [...g.turns, { role: "user", text: "Find me a pasta recipe" }] } } });
  const after = (ms: number) => {
    setState({ awaiting: { to: "gnsis", seq: 1, since: 50_000, state: "accepted" }, now: 50_000 + ms });
    return renderToStaticMarkup(createElement(ChatWindow, { height: "auto" }));
  };
  assert.match(after(0), /<span class="thinking-time" aria-hidden="true">1s ·<\/span><span class="shimmer">Thinking<\/span>/, "from the first second");
  assert.match(after(4_999), />4s ·<\/span><span class="shimmer">Thinking<\/span>/);
  assert.match(after(5_000), />5s ·<\/span><span class="shimmer">Thinking some more<\/span>/);
  assert.match(after(83_000), />83s ·<\/span><span class="shimmer">Thinking some more<\/span>/);
});

test("the ≡ menu offers “Hide to menu bar” only where GNSIS has a menu bar icon", () => {
  resetStore();
  configure(new SimulatedLiveHost({ text: false, transcript: false }), ids);
  enterDesktop(identity, false);
  assert.doesNotMatch(renderToStaticMarkup(createElement(DockMenu, { left: 0 })), /Hide to menu bar/);
  resetStore();
  configure(new SimulatedLiveHost({ text: false, transcript: false, menuBarIcon: () => ({ x: 1390, y: -16 }) }), ids);
  enterDesktop(identity, false);
  assert.match(renderToStaticMarkup(createElement(DockMenu, { left: 0 })), /Hide to menu bar/);
});

test("the ≡ menu offers “Quit GNSIS” only where the host can close GNSIS", () => {
  resetStore();
  configure(new SimulatedLiveHost({ text: false, transcript: false }), ids);
  enterDesktop(identity, false);
  assert.doesNotMatch(renderToStaticMarkup(createElement(DockMenu, { left: 0 })), /Quit GNSIS/);
  resetStore();
  configure(new SimulatedLiveHost({ text: false, transcript: false, quit: () => {} }), ids);
  enterDesktop(identity, false);
  assert.match(renderToStaticMarkup(createElement(DockMenu, { left: 0 })), /Quit GNSIS/);
});

test("the chat shows the person's words only: no voice tags, bars or times", () => {
  resetStore();
  configure(new SimulatedLiveHost({ text: false, transcript: false }), ids);
  enterDesktop(identity, false);
  actions.openAgent("gnsis");
  const g = getState().convs.gnsis;
  setState({
    convs: {
      ...getState().convs,
      gnsis: {
        ...g,
        turns: [
          ...g.turns,
          { role: "user", text: "", spoken: true, spokenMs: 4000 },
          { role: "user", text: "Open YouTube", spoken: true },
          { role: "agent", text: "Sure, opening YouTube in Chrome.", stream: 99 },
          { role: "system", text: "Done: Google Chrome opened a new tab at youtube.com; it now has 2 tabs." },
        ],
      },
    },
  });
  const chat = renderToStaticMarkup(createElement(ChatWindow, { height: "auto" }));
  assert.match(chat, />Open YouTube</);
  assert.doesNotMatch(chat, /Spoken|Listening|0:04|spoken-bars/);
  assert.match(chat, /<div class="system-line">Done: Google Chrome opened a new tab/, "an activity line carries no voice icon");
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

test("the Activity panel shows what is shared with GNSIS: nothing made up, the real picture once it is shared", async () => {
  const { setState } = await import("../store/store");
  const { ScreenCard } = await import("./ScreenView");
  const render = () => renderToStaticMarkup(createElement(ScreenCard));
  resetStore();
  const host = new SimulatedLiveHost({ screen: true, camera: true });
  configure(host, ids);
  enterDesktop(identity, false);
  setState({ link: "ready" });

  const off = render();
  assert.match(off, /Shared with GNSIS/);
  assert.match(off, /You aren’t sharing your screen or camera with GNSIS\./);
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
  assert.match(on, />Your screen</);
  assert.doesNotMatch(on, /smaller still pictures/, "the owner's call (30 September)");
  assert.match(on, /Stop sharing/);

  // The connection drops: the picture is still shared locally, but it is not reaching GNSIS.
  setState({ link: "closed" });
  const paused = render();
  assert.match(paused, /Paused: GNSIS isn’t connected, so it isn’t getting your screen/);
  assert.doesNotMatch(paused, /smaller still pictures/);
  setState({ link: "ready" });

  // On, but this host cannot hand over its picture: say so, never show a stand-in.
  host.visionStream = () => null;
  const blind = render();
  assert.doesNotMatch(blind, /<video/);
  assert.match(blind, /can’t show you the picture here/);

  setState({ vision: { source: "screen", state: "denied", detail: "macOS hasn’t allowed GNSIS to record your screen." } });
  const denied = render();
  assert.match(denied, /macOS hasn’t allowed GNSIS to record your screen\./);
  assert.match(denied, /Try sharing again/);
  assert.match(denied, /Use camera/);

  // A camera problem is retried on the camera, never by sharing the screen instead.
  setState({ vision: { source: "camera", state: "error", detail: "The camera is in use by another app." } });
  const camera = render();
  assert.match(camera, /Try the camera again/);
  assert.match(camera, /Share my screen/, "sharing the screen stays its own, plainly named button");
  assert.doesNotMatch(camera, /Try sharing again/);
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
