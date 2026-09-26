import assert from "node:assert/strict";
import { test } from "node:test";
import { SimulatedLiveHost } from "../hosts/simulated";
import type { Identity, IdentityStore, LiveEvent, LiveHost } from "../host";
import { dockGeometry, rankedAgents } from "../components/Shell";
import { actions, configure, enterDesktop, getState, presence, resetStore, setState, tick } from "./store";
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
  assert.match(getState().convs.gnsis.turns[0].text, /Press the voice button to talk with me live\. When/);
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
