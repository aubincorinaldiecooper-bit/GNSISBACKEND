import assert from "node:assert/strict";
import { test } from "node:test";
import { SimulatedLiveHost } from "../hosts/simulated";
import type { Identity, IdentityStore, LiveEvent, LiveHost } from "../host";
import { dockGeometry, rankedAgents } from "../components/Shell";
import {
  CLOSED_BEFORE_ANSWER, NO_ANSWER_YET, REPLY_TIMEOUT_MS, TYPING_LINK_DOWN, TYPING_WHILE_CONNECTING, TYPING_WHILE_LIVE,
  actions, activity, configure, enterDesktop, eraseIdentity, filteredCommands, getState, presence, resetStore, setState, tick,
} from "./store";
import { TYPING_NOT_CONNECTED } from "../demo/data";

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

const identity: Identity = { publicId: "gnsis:TEST-TEST-TEST", publicKey: "", storage: "local" };
const identityStore: IdentityStore = {
  storageNote: "test",
  load: async () => identity,
  create: async () => identity,
  erase: async () => {},
};

/** A host that records what the UI asked of it and lets a test push events. */
class RecordingHost implements LiveHost {
  readonly kind = "recording";
  calls: string[] = [];
  text = false;
  private listeners = new Set<(e: LiveEvent) => void>();
  capabilities() {
    return { voice: true, text: this.text, screen: true, camera: false, transcript: false, overlay: false };
  }
  subscribe(fn: (e: LiveEvent) => void) {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }
  push(e: LiveEvent) {
    for (const l of this.listeners) l(e);
  }
  async startLive() { this.calls.push("startLive"); }
  async endLive() { this.calls.push("endLive"); }
  async setMuted(m: boolean) { this.calls.push(`setMuted:${m}`); }
  async startVision(source: "screen" | "camera") { this.calls.push(`startVision:${source}`); }
  async stopVision() { this.calls.push("stopVision"); }
  /** How the host answers a typed message: accepted by default. */
  textResult: () => Promise<void> = async () => {};
  async sendText(text: string) { this.calls.push(`sendText:${text}`); return this.textResult(); }
}

test("the dock ranks needs you, working, new result, done, and folds the rest into +N", () => {
  resetStore();
  configure(new RecordingHost(), identityStore);
  enterDesktop(identity, true);
  const s = getState();
  const order = rankedAgents(s).map((r) => `${r.id}:${r.p.status}`);
  assert.deepEqual(order, ["roof:Needs you", "recipe:Working", "watch:Watching", "triage:New result", "gift:Done earlier"]);
  const g = dockGeometry(s);
  assert.deepEqual(g.shown.map((r) => r.id), ["roof", "recipe", "watch", "triage"], "the archived agent has left the dock");
  assert.equal(g.more, 0);
  // Five unarchived agents: four shown, one behind +1.
  setState((st) => ({ convs: { ...st.convs, gift: { ...st.convs.gift, archived: false } } }));
  const g2 = dockGeometry(getState());
  assert.equal(g2.more, 1);
  assert.equal(g2.width, 28 + 64 + 21 + 64 * 4 + 52 + 21 + 48 + 52 + 4 * 9 + 2);
  assert.equal(presence(getState().convs.gift, getState()).status, "Done");
});

test("live voice: the host's words land in the chat and ending it writes the closing line", async () => {
  resetStore();
  const host = new RecordingHost();
  configure(host, identityStore);
  enterDesktop(identity, false);
  actions.startLive("gnsis");
  await sleep(0);
  assert.deepEqual(host.calls, ["startLive"]);
  let s = getState();
  assert.ok(s.live && s.live.to === "gnsis" && s.mode === "bar" && s.winOpen);
  host.push({ type: "link", state: "ready" });
  assert.equal(getState().live?.phase, "connecting", "a ready link is not yet a listening microphone");
  host.push({ type: "mic", state: "on" });
  assert.equal(getState().live?.phase, "listening");
  host.push({ type: "agent.speaking", speaking: true });
  host.push({ type: "agent.text", text: "Hi! I’m here.", endOfTurn: false, interrupted: false });
  s = getState();
  assert.equal(s.live?.agent?.text, "Hi! I’m here.");
  host.push({ type: "agent.text", text: " What’s on your mind?", endOfTurn: true, interrupted: false });
  host.push({ type: "agent.speaking", speaking: false });
  host.push({ type: "user.speech", state: "start" });
  host.push({ type: "user.speech", state: "end", ms: 2500 });
  s = getState();
  const turns = s.convs.gnsis.turns;
  const said = "Hi! I’m here. What’s on your mind?";
  assert.deepEqual(turns.slice(-2), [
    { role: "agent", text: said, stream: said.length },
    { role: "user", text: "", spoken: true, spokenMs: 2500 },
  ]);
  actions.toggleMute();
  assert.equal(getState().live?.muted, true);
  assert.equal(host.calls.at(-1), "setMuted:true");
  actions.endLive();
  s = getState();
  assert.equal(s.live, null);
  assert.equal(host.calls.at(-1), "endLive");
  assert.match(s.convs.gnsis.turns.at(-1)!.text, /^Live conversation · \d+:\d\d$/);
});

test("switching to another chat or going back to the dock ends live", async () => {
  resetStore();
  const host = new RecordingHost();
  configure(host, identityStore);
  enterDesktop(identity, true);
  actions.startLive("gnsis");
  await sleep(0);
  actions.openAgent("roof");
  assert.equal(getState().live, null);
  assert.equal(host.calls.at(-1), "endLive");
  actions.startLive("roof");
  await sleep(0);
  actions.toDock();
  assert.equal(getState().live, null);
  assert.equal(getState().mode, "dock");
  assert.equal(host.calls.filter((c) => c === "endLive").length, 2);
});

test("when the host cannot start live, the chat says so and nothing stays armed", async () => {
  resetStore();
  const host = new RecordingHost();
  host.startLive = async () => { throw new Error("The microphone was not allowed."); };
  configure(host, identityStore);
  enterDesktop(identity, false);
  actions.startLive();
  await sleep(0);
  const s = getState();
  assert.equal(s.live, null);
  const last = s.convs.gnsis.turns.at(-1)!;
  assert.equal(last.role, "system");
  assert.equal(last.text, "Live voice couldn’t start. The microphone was not allowed.");
});

test("the visual sense is a switch the host reports back on", async () => {
  resetStore();
  const host = new RecordingHost();
  configure(host, identityStore);
  enterDesktop(identity, false);
  actions.setVision("camera");
  assert.deepEqual(host.calls, [], "a source the host cannot provide is never asked for");
  actions.setVision("screen");
  await sleep(0);
  assert.deepEqual(host.calls, ["startVision:screen"]);
  assert.deepEqual(getState().vision, { source: "screen", state: "starting" });
  host.push({ type: "vision", source: "screen", state: "on" });
  assert.equal(getState().vision.state, "on");
  // /screen in the bar toggles it.
  setState({ text: "/screen", mode: "bar", winOpen: true });
  actions.send();
  await sleep(0);
  assert.equal(host.calls.at(-1), "stopVision");
});

test("typing to a host that cannot deliver it gets an honest line, never a made-up reply", () => {
  resetStore();
  const host = new RecordingHost();
  configure(host, identityStore);
  enterDesktop(identity, false);
  // The welcome invites only what works here: no typing, and no agents the real app never starts.
  const welcome = getState().convs.gnsis.turns[0].text;
  assert.equal(welcome, "Hi, I’m your GNSIS. Press the voice button to talk with me live.");
  setState({ text: "What’s the weather tomorrow?", mode: "bar", winOpen: true });
  actions.send();
  const s = getState();
  assert.deepEqual(s.convs.gnsis.turns.slice(-2), [
    { role: "user", text: "What’s the weather tomorrow?" },
    { role: "system", text: TYPING_NOT_CONNECTED },
  ]);
  assert.equal(s.agentIds.length, 0, "no stand-in agent was started");
  assert.equal(s.text, "");
  // In demo mode the stand-ins are back, so the whole flow can be reviewed.
  enterDesktop(identity, true);
  setState({ text: "Find me a plumber", mode: "bar", winOpen: true, active: "gnsis" });
  actions.send();
  assert.ok(getState().agentIds.includes("c1"), "demo mode hands the job to a stand-in agent");
});

test("when nothing is moving, the clock stands still and nothing re-renders", () => {
  resetStore();
  configure(new RecordingHost(), identityStore);
  enterDesktop(identity, false);
  const before = getState();
  tick();
  tick();
  assert.equal(getState(), before, "an idle desktop does not produce new state");
  // Something to do: a stand-in reply streaming in.
  setState((s) => ({ convs: { ...s.convs, gnsis: { ...s.convs.gnsis, turns: [...s.convs.gnsis.turns, { role: "agent", text: "Working on it.", stream: 0 }] } } }));
  const busyBefore = getState();
  tick();
  assert.notEqual(getState(), busyBefore);
  assert.equal(getState().t, busyBefore.t + 1);
});

test("the simulated host plays a whole conversation into the chat", async () => {
  resetStore();
  const host = new SimulatedLiveHost({ tenthMs: 1 });
  configure(host, identityStore);
  enterDesktop(identity, false);
  actions.startLive("gnsis");
  await sleep(5);
  assert.equal(getState().live?.phase, "connecting", "a moment of connecting before the microphone is on");
  await sleep(450);
  const s = getState();
  assert.equal(s.live?.phase, "listening");
  const turns = s.convs.gnsis.turns.slice(1); // after the greeting
  const agent = turns.filter((t) => t.role === "agent").map((t) => t.text);
  const user = turns.filter((t) => t.role === "user").map((t) => t.text);
  const lines = turns.filter((t) => t.role === "system").map((t) => t.text);
  assert.equal(agent[0], "Hi! I’m here. What’s on your mind?");
  assert.ok(agent.some((t) => t.endsWith("—")), `an interrupted reply ends on a dash: ${agent}`);
  assert.equal(user[0], "Open YouTube and search for pasta recipes.");
  // What GNSIS did lands as lines: the outcomes, and the step it asked about.
  assert.deepEqual(lines, [
    "Done: Google Chrome opened a new tab at youtube.com; it now has 2 tabs.",
    "Done: Typed it. Look at the screen to check it went where it should.",
    "Waiting for your OK: Press enter",
    "Done: Pressed enter. Look at the screen to see what it did.",
  ]);
  assert.equal(s.asking, null);
  assert.equal(s.working, null);
  assert.ok(turns.every((t) => t.role !== "user" || (t.spoken && t.spokenMs !== undefined)), "spoken turns carry their length");
  actions.endLive();
  assert.equal(getState().live, null);
});

test("the Activity drawer is hidden until the person opens it; an agent that needs them marks it, never opens it", () => {
  resetStore();
  configure(new RecordingHost(), identityStore);
  enterDesktop(identity, false);
  actions.openAgent("gnsis");
  let s = getState();
  assert.equal(s.winOpen, true);
  assert.equal(s.panelHidden, true, "the chat opens alone, with no empty agents panel beside it");
  actions.toggleActivity();
  assert.equal(getState().panelHidden, false);
  actions.toggleActivity();
  assert.equal(getState().panelHidden, true);
  // Opening a chat, or starting live voice, leaves the drawer as the person left it.
  actions.startLive("gnsis");
  assert.equal(getState().panelHidden, true);
  actions.endLive();
  // Demo agents: one needs the person, one is working, one has news.
  enterDesktop(identity, true);
  actions.openAgent("gnsis");
  s = getState();
  assert.deepEqual(activity(s), { needs: 1, working: 2, unread: 1 });
  assert.equal(s.panelHidden, true, "an agent needing the person does not force the drawer open");
  // Opening an agent's chat is asking to see it: its work shows beside it.
  actions.openAgent("roof");
  assert.equal(getState().panelHidden, false);
  // Back in GNSIS's own chat, the drawer is closed again.
  actions.openAgent("gnsis");
  assert.equal(getState().panelHidden, true);
});

test("in the real app, GNSIS's own actions light the Activity button: waiting for an OK, and running", () => {
  resetStore();
  const host = new RecordingHost();
  configure(host, identityStore);
  enterDesktop(identity, false);
  actions.openAgent("gnsis");
  assert.deepEqual(activity(getState()), { needs: 0, working: 0, unread: 0 });
  host.push({ type: "action", state: "waiting", text: "Open youtube.com in a new tab in Google Chrome" });
  assert.deepEqual(getState().asking, { to: "gnsis", text: "Open youtube.com in a new tab in Google Chrome" });
  assert.deepEqual(activity(getState()), { needs: 1, working: 0, unread: 0 });
  host.push({ type: "action", state: "working", text: "Open youtube.com in a new tab in Google Chrome" });
  assert.equal(getState().asking, null);
  assert.deepEqual(activity(getState()), { needs: 0, working: 1, unread: 0 });
  host.push({ type: "action", state: "done", text: "Google Chrome opened a new tab at youtube.com." });
  assert.deepEqual(activity(getState()), { needs: 0, working: 0, unread: 0 });
  assert.equal(getState().panelHidden, true, "none of it opens the drawer");
});

test("typing: the message shows at once, goes to GNSIS through the host, and GNSIS's answer lands in the chat", async () => {
  resetStore();
  const host = new RecordingHost();
  host.text = true;
  let accept!: () => void;
  host.textResult = () => new Promise<void>((resolve) => (accept = resolve));
  configure(host, identityStore);
  enterDesktop(identity, false);
  setState({ text: "Open YouTube and search Andrew Tate", mode: "bar", winOpen: true });
  actions.send();
  let s = getState();
  assert.deepEqual(host.calls, ["sendText:Open YouTube and search Andrew Tate"]);
  assert.deepEqual(s.convs.gnsis.turns.at(-1), { role: "user", text: "Open YouTube and search Andrew Tate" });
  assert.equal(s.text, "");
  assert.equal(s.awaiting?.state, "sent");
  assert.ok(!s.convs.gnsis.turns.some((t) => t.text === TYPING_NOT_CONNECTED));
  assert.equal(s.agentIds.length, 0, "no stand-in agent, no canned reply");
  accept();
  await sleep(0);
  assert.equal(getState().awaiting?.state, "accepted");
  // GNSIS acts on the computer: the start is shown while it runs, the outcome lands as a line.
  host.push({ type: "action", state: "working", text: "Open a new tab in Google Chrome" });
  assert.deepEqual(getState().working, { to: "gnsis", text: "Open a new tab in Google Chrome" });
  host.push({ type: "action", state: "done", text: "Google Chrome is on youtube.com." });
  s = getState();
  assert.equal(s.working, null);
  assert.deepEqual(s.convs.gnsis.turns.at(-1), { role: "system", text: "Done: Google Chrome is on youtube.com." });
  assert.equal(s.awaiting?.state, "accepted", "what GNSIS did is not its answer: the message stays open");
  // Its words arrive in pieces and become one reply.
  host.push({ type: "agent.text", text: "Here are the", endOfTurn: false, interrupted: false });
  assert.deepEqual(getState().reply, { to: "gnsis", text: "Here are the" });
  host.push({ type: "agent.text", text: " results.", endOfTurn: true, interrupted: false });
  s = getState();
  assert.equal(s.reply, null);
  assert.equal(s.awaiting, null);
  assert.deepEqual(s.convs.gnsis.turns.at(-1), { role: "agent", text: "Here are the results.", stream: 21 });
});

test("a typed message that could not be sent says why, in plain words", async () => {
  resetStore();
  const host = new RecordingHost();
  host.text = true;
  host.textResult = async () => { throw new Error("GNSIS isn’t connected right now. Try again in a moment."); };
  configure(host, identityStore);
  enterDesktop(identity, false);
  setState({ text: "Open YouTube", mode: "bar", winOpen: true });
  actions.send();
  await sleep(0);
  const s = getState();
  assert.equal(s.awaiting, null);
  assert.deepEqual(s.convs.gnsis.turns.slice(-2), [
    { role: "user", text: "Open YouTube" },
    { role: "system", text: "Not sent. GNSIS isn’t connected right now. Try again in a moment." },
  ]);
});

test("a typed message GNSIS accepts but never answers is said so, not left hanging", async () => {
  resetStore();
  const host = new RecordingHost();
  host.text = true;
  configure(host, identityStore);
  enterDesktop(identity, false);
  setState({ text: "Open YouTube", mode: "bar", winOpen: true });
  actions.send();
  await sleep(0);
  assert.equal(getState().awaiting?.state, "accepted");
  setState((st) => ({ awaiting: { ...st.awaiting!, since: Date.now() - REPLY_TIMEOUT_MS - 1 } }));
  tick();
  const s = getState();
  assert.equal(s.awaiting, null);
  assert.deepEqual(s.convs.gnsis.turns.at(-1), { role: "system", text: NO_ANSWER_YET });
});

test("outside live voice, what GNSIS did still lands in the chat; stray words of a reply that was cut off do not", async () => {
  resetStore();
  const host = new RecordingHost();
  configure(host, identityStore);
  enterDesktop(identity, false);
  actions.startLive("gnsis");
  host.push({ type: "mic", state: "on" });
  host.push({ type: "action", state: "waiting", text: "Waiting for your OK: Move “report.pdf” into “Projects”" });
  actions.endLive();
  const before = getState().convs.gnsis.turns.length;
  // The person approved after ending the call: the outcome is not lost.
  host.push({ type: "action", state: "working", text: "Move “report.pdf” into “Projects”" });
  host.push({ type: "action", state: "done", text: "Moved report.pdf into ~/Documents/Projects." });
  // The tail of the reply they ended is not shown as a new answer.
  host.push({ type: "agent.text", text: "…and then I", endOfTurn: true, interrupted: false });
  const s = getState();
  assert.deepEqual(s.convs.gnsis.turns.slice(before), [{ role: "system", text: "Done: Moved report.pdf into ~/Documents/Projects." }]);
  assert.equal(s.reply, null);
});

test("a real build offers only the commands that do something, and never starts a stand-in agent", () => {
  resetStore();
  const host = new RecordingHost();
  configure(host, identityStore);
  enterDesktop(identity, false);
  assert.deepEqual(filteredCommands("/").map((c) => c.name), ["/screen"]);
  setState({ text: "/browse recipes.example", mode: "bar", winOpen: true });
  actions.send();
  let s = getState();
  assert.equal(s.agentIds.length, 0, "no stand-in browser agent");
  assert.deepEqual(s.convs.gnsis.turns.slice(-2), [
    { role: "user", text: "/browse recipes.example" },
    { role: "system", text: TYPING_NOT_CONNECTED },
  ]);
  // Even a host that takes typed messages gets no stand-ins outside demo mode.
  host.text = true;
  host.push({ type: "link", state: "ready" });
  assert.equal(getState().caps.text, true, "the store re-reads what the host can do when the link changes");
  setState({ text: "Find me a plumber", mode: "bar", winOpen: true, active: "gnsis" });
  actions.send();
  s = getState();
  assert.equal(s.agentIds.length, 0);
  assert.equal(host.calls.at(-1), "sendText:Find me a plumber");
  // Settings cannot switch the real app into demo mode either.
  actions.loadDemo();
  s = getState();
  assert.equal(s.demo, false);
  assert.equal(s.agentIds.length, 0, "no sample agents in the real app");
  // Demo mode keeps every command, for design review.
  enterDesktop(identity, true);
  assert.equal(filteredCommands("/").length, 5);
});

/** A desktop with a host that takes typed messages, accepted when the test says so. */
function typingDesk() {
  resetStore();
  const host = new RecordingHost();
  host.text = true;
  const pending: Array<{ text: string; accept(): void; reject(e: Error): void }> = [];
  host.textResult = () => new Promise<void>((accept, reject) => pending.push({ text: host.calls.at(-1)!.slice("sendText:".length), accept, reject }));
  configure(host, identityStore);
  enterDesktop(identity, false);
  host.push({ type: "link", state: "ready" });
  actions.openAgent("gnsis");
  const type = (text: string) => { setState({ text }); actions.send(); };
  return { host, pending, type };
}

test("typing: GNSIS's words after it acts still land — a preamble, the action, then the result", async () => {
  const { host, pending, type } = typingDesk();
  type("Open YouTube");
  pending[0].accept();
  await sleep(0);
  host.push({ type: "agent.text", text: "Sure, opening it.", endOfTurn: true, interrupted: false });
  host.push({ type: "action", state: "working", text: "Open youtube.com in a new tab in Google Chrome" });
  host.push({ type: "action", state: "done", text: "Google Chrome opened a new tab at youtube.com." });
  host.push({ type: "agent.text", text: "It is open.", endOfTurn: true, interrupted: false });
  assert.deepEqual(getState().convs.gnsis.turns.slice(-4).map((t) => t.text), [
    "Open YouTube", "Sure, opening it.", "Done: Google Chrome opened a new tab at youtube.com.", "It is open.",
  ]);
});

test("typing: each message keeps its own place — one failing never changes what is said about the next", async () => {
  const { pending, type } = typingDesk();
  type("first");
  type("second");
  pending[0].reject(Object.assign(new Error("GNSIS didn’t confirm it got your message. Try again in a moment."), { unconfirmed: true }));
  await sleep(0);
  let s = getState();
  assert.equal(s.awaiting?.state, "sent", "the second message is still on its way");
  assert.deepEqual(s.convs.gnsis.turns.at(-1), { role: "system", text: "GNSIS didn’t confirm it got your message. Try again in a moment." }, "a message that went out is not called “not sent”");
  pending[1].accept();
  await sleep(0);
  s = getState();
  assert.equal(s.awaiting?.state, "accepted");
});

test("typing: the no-answer line waits while GNSIS is working, and counts from when the runtime had the message", async () => {
  const { host, pending, type } = typingDesk();
  type("Open YouTube");
  setState((st) => ({ awaiting: { ...st.awaiting!, since: Date.now() - REPLY_TIMEOUT_MS - 5_000 } }));
  pending[0].accept();
  await sleep(0);
  tick();
  assert.equal(getState().awaiting?.state, "accepted", "a slow send does not use up the wait");
  host.push({ type: "action", state: "working", text: "Open a new tab" });
  setState((st) => ({ awaiting: { ...st.awaiting!, since: Date.now() - REPLY_TIMEOUT_MS - 1 } }));
  tick();
  assert.ok(!getState().convs.gnsis.turns.some((t) => t.text === NO_ANSWER_YET), "not while the action runs");
  host.push({ type: "action", state: "done", text: "Opened a new tab." });
  setState((st) => ({ awaiting: { ...st.awaiting!, since: Date.now() - REPLY_TIMEOUT_MS - 1 } }));
  tick();
  assert.equal(getState().convs.gnsis.turns.at(-1)?.text, NO_ANSWER_YET);
});

test("typing: a connection that closes before GNSIS answers says so, and keeps what had arrived", async () => {
  const { host, pending, type } = typingDesk();
  type("Open YouTube");
  pending[0].accept();
  await sleep(0);
  host.push({ type: "agent.text", text: "Let me open that for", endOfTurn: false, interrupted: false });
  host.push({ type: "link", state: "closed", detail: "The session ended." });
  const s = getState();
  assert.equal(s.awaiting, null);
  assert.equal(s.reply, null);
  assert.deepEqual(s.convs.gnsis.turns.slice(-2).map((t) => t.text), ["Let me open that—", CLOSED_BEFORE_ANSWER]);
});

test("typing: a reply still arriving is never lost — not to live voice, and not to the next message", async () => {
  const { host, pending, type } = typingDesk();
  type("Open YouTube");
  pending[0].accept();
  await sleep(0);
  host.push({ type: "agent.text", text: "Let me", endOfTurn: false, interrupted: false });
  // Live voice starts mid-reply: the reply carries on as the live one.
  actions.startLive("gnsis");
  host.push({ type: "mic", state: "on" });
  host.push({ type: "agent.text", text: " check that.", endOfTurn: true, interrupted: false });
  assert.equal(getState().convs.gnsis.turns.at(-1)?.text, "Let me check that.");
  actions.endLive();
  assert.equal(getState().reply, null, "no stale reply is left blinking after the call");
  // A second message mid-reply keeps the first reply as it stands.
  type("And search Andrew Tate");
  pending[1].accept();
  await sleep(0);
  host.push({ type: "agent.text", text: "Searching now", endOfTurn: false, interrupted: false });
  type("Thanks");
  const turns = getState().convs.gnsis.turns.slice(-3).map((t) => t.text);
  assert.deepEqual(turns, ["And search Andrew Tate", "Searching now", "Thanks"]);
});

test("typing that cannot go is explained for the moment the person is in", async () => {
  resetStore();
  const host = new RecordingHost();
  configure(host, identityStore);
  enterDesktop(identity, false);
  const last = () => getState().convs.gnsis.turns.at(-1)?.text;
  // Live and listening: never point at the button that would end the call.
  actions.startLive("gnsis");
  setState({ text: "open youtube" });
  actions.send();
  assert.equal(last(), TYPING_WHILE_CONNECTING);
  host.push({ type: "mic", state: "on" });
  setState({ text: "open youtube" });
  actions.send();
  assert.equal(last(), TYPING_WHILE_LIVE);
  actions.endLive();
  // A runtime that takes typing, with the link down: the link is what is missing.
  host.text = true;
  host.push({ type: "link", state: "ready" });
  host.text = false;
  host.push({ type: "link", state: "closed", detail: "The session ended." });
  setState({ text: "open youtube", mode: "bar", winOpen: true });
  actions.send();
  assert.equal(last(), TYPING_LINK_DOWN);
});

test("news for a closed chat marks it and says so", () => {
  resetStore();
  const host = new RecordingHost();
  configure(host, identityStore);
  enterDesktop(identity, false);
  actions.openAgent("gnsis");
  actions.closeTab("gnsis");
  host.push({ type: "action", state: "failed", text: "Google Chrome did not open a new tab." });
  const s = getState();
  assert.equal(s.convs.gnsis.unread, true);
  assert.deepEqual(s.toast && { id: s.toast.id, text: s.toast.text }, { id: "gnsis", text: "Couldn’t do it: Google Chrome did not open a new tab." });
  actions.openAgent("gnsis");
  assert.equal(getState().convs.gnsis.unread, false);
});

test("erasing GNSIS stops what it was seeing before the welcome screen shows", async () => {
  resetStore();
  const host = new RecordingHost();
  configure(host, identityStore);
  enterDesktop(identity, false);
  actions.setVision("screen");
  host.push({ type: "vision", source: "screen", state: "on" });
  assert.equal(await eraseIdentity(), null);
  const s = getState();
  assert.ok(host.calls.includes("stopVision"));
  assert.deepEqual(s.vision, { source: null, state: "off" });
  assert.equal(s.phase, "welcome");
});

test("screen sharing that never gets a picture is stopped, and the reason shows in the chat", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  resetStore();
  const host = new RecordingHost();
  configure(host, identityStore);
  enterDesktop(identity, false);
  actions.openAgent("gnsis");
  actions.setVision("screen");
  t.mock.timers.tick(20_000);
  await Promise.resolve();
  await Promise.resolve();
  const s = getState();
  assert.ok(host.calls.includes("stopVision"), "the capture is stopped, not left running behind an error");
  assert.equal(s.vision.state, "error");
  assert.match(s.convs.gnsis.turns.at(-1)?.text ?? "", /sharing your screen was stopped/);
});
