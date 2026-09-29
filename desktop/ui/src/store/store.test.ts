import assert from "node:assert/strict";
import { test } from "node:test";
import { SimulatedLiveHost } from "../hosts/simulated";
import type { Identity, IdentityStore, LiveEvent, LiveHost } from "../host";
import { dockGeometry, rankedAgents } from "../components/Shell";
import { NO_ANSWER_YET, REPLY_TIMEOUT_MS, actions, activity, configure, enterDesktop, filteredCommands, getState, presence, resetStore, setState, tick } from "./store";
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
  await sleep(450);
  const s = getState();
  const turns = s.convs.gnsis.turns.slice(1); // after the greeting
  const agent = turns.filter((t) => t.role === "agent").map((t) => t.text);
  const user = turns.filter((t) => t.role === "user").map((t) => t.text);
  assert.equal(agent[0], "Hi! I’m here. What’s on your mind?");
  assert.ok(agent.some((t) => t.endsWith("—")), `an interrupted reply ends on a dash: ${agent}`);
  assert.equal(user[0], "I’m meeting the roofer tomorrow, and I’m a little nervous about it.");
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
  actions.openAgent("roof");
  assert.equal(getState().panelHidden, true);
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
  // Its words arrive in pieces and become one reply.
  setState({ awaiting: { to: "gnsis", since: Date.now(), state: "accepted" } });
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
