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
