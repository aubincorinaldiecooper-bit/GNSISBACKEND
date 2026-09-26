import { useSyncExternalStore } from "react";
import {
  AGENT_ANSWER, AGENT_TOTAL, APPROVAL, COMMANDS, FOLLOW_PHRASE, QUESTION, ROOF_DRAFT, TYPING_NOT_CONNECTED,
  demoAgents, focusConv, homeConv, recipeConv, spawnedConv,
  type Conv, type Turn,
} from "../demo/data";
import type { HostCapabilities, Identity, IdentityStore, LinkState, LiveEvent, LiveHost, VisionSource } from "../host";
import { applyLiveEvent, endLiveTurns, liveInfo, openTurns, startLiveState, type LiveInfo, type LiveState } from "./live";

export interface Toast { id: string; text: string; ttl: number }

export interface VisionState {
  source: VisionSource | null;
  state: "off" | "starting" | "on" | "denied" | "error";
  detail?: string;
}

export interface State {
  phase: "loading" | "welcome" | "ready" | "desktop";
  creating: boolean;
  identity: Identity | null;
  caps: HostCapabilities;
  mode: "dock" | "bar";
  convs: Record<string, Conv>;
  agentIds: string[];
  tabs: string[];
  active: string;
  winOpen: boolean;
  listening: boolean;
  listenTo: string | null;
  heard: number;
  hold: number;
  text: string;
  /** stand-in clock, in ticks of TICK_MS; drives the demo agents */
  t: number;
  /** wall clock, refreshed each tick while live voice is on */
  now: number;
  seq: number;
  panelHidden: boolean;
  menuOpen: boolean;
  dockMenu: boolean;
  visionMenu: boolean;
  settingsOpen: boolean;
  toast: Toast | null;
  apPick: number | null;
  apCustom: string;
  live: LiveState | null;
  /** the runtime link as last reported by the host, live or not */
  link: LinkState;
  vision: VisionState;
  greet: boolean;
  /** show the sample agents so every state can be reviewed */
  demo: boolean;
}

const NO_CAPS: HostCapabilities = { voice: false, text: false, screen: false, camera: false, transcript: false, overlay: false };

/** How long the visual sense may sit on "starting" before that is reported as a problem. */
const VISION_START_TIMEOUT_MS = 20_000;

const initial: State = {
  phase: "loading", creating: false, identity: null, caps: NO_CAPS,
  mode: "dock", convs: {}, agentIds: [], tabs: ["gnsis"], active: "gnsis", winOpen: false,
  listening: false, listenTo: null, heard: 0, hold: 0, text: "", t: 0, now: Date.now(), seq: 0,
  panelHidden: false, menuOpen: false, dockMenu: false, visionMenu: false, settingsOpen: false,
  toast: null, apPick: null, apCustom: "", live: null, link: "connecting",
  vision: { source: null, state: "off" }, greet: false, demo: false,
};

// ---- tiny store -------------------------------------------------------------
let state: State = initial;
const listeners = new Set<() => void>();
export const getState = () => state;
export function setState(patch: Partial<State> | ((s: State) => Partial<State>)) {
  const p = typeof patch === "function" ? patch(state) : patch;
  state = { ...state, ...p };
  listeners.forEach((l) => l());
}
function subscribe(l: () => void) {
  listeners.add(l);
  return () => listeners.delete(l);
}
export function useStore<T>(select: (s: State) => T): T {
  return useSyncExternalStore(subscribe, () => select(state));
}
/** Tests only: back to the initial state, host and all. */
export function resetStore() {
  unsubscribeHost?.();
  unsubscribeHost = null;
  host = null;
  identityStore = null;
  state = { ...initial, now: Date.now() };
  listeners.forEach((l) => l());
}

// ---- the host ---------------------------------------------------------------
let host: LiveHost | null = null;
let identityStore: IdentityStore | null = null;
let unsubscribeHost: (() => void) | null = null;

/** Hand the UI its host. Called once when the app mounts; safe to call again with a new host. */
export function configure(h: LiveHost, ids: IdentityStore) {
  unsubscribeHost?.();
  host = h;
  identityStore = ids;
  setState({ caps: h.capabilities() });
  unsubscribeHost = h.subscribe(onLiveEvent);
}
export const getHost = () => host;
export const getIdentityStore = () => identityStore;

let visionTimer: ReturnType<typeof setTimeout> | null = null;

function onLiveEvent(ev: LiveEvent) {
  if (ev.type === "vision") {
    if (ev.state !== "starting" && visionTimer) {
      clearTimeout(visionTimer);
      visionTimer = null;
    }
    setState({ vision: { source: ev.source, state: ev.state, detail: ev.detail } });
    return;
  }
  if (ev.type === "link") setState({ link: ev.state });
  let ended: string | undefined;
  setState((s) => {
    if (!s.live) return {};
    const step = applyLiveEvent(s.live, ev, Date.now());
    const c = s.convs[s.live.to];
    const convs = step.commit.length && c ? { ...s.convs, [c.id]: { ...c, turns: [...c.turns, ...step.commit] } } : s.convs;
    if (step.ended) ended = step.ended.reason;
    return { live: step.live, convs };
  });
  if (ended !== undefined) finishLive(ended);
}

// ---- helpers ----------------------------------------------------------------
const withConv = (s: State, id: string, patch: Partial<Conv>) => ({ ...s.convs, [id]: { ...s.convs[id], ...patch } });
const withTab = (tabs: string[], id: string) => (tabs.includes(id) ? tabs : [...tabs, id]);

export function isWorking(c?: Conv): boolean {
  if (!c || c.stopped) return false;
  const lt = c.turns.length ? c.turns[c.turns.length - 1] : null;
  if (lt && lt.role === "agent" && (lt.stream ?? 0) < lt.text.length) return true;
  if (c.forever) return true;
  if (c.kind === "agent") return (c.agentT ?? 0) < AGENT_TOTAL || (c.agentStream ?? 0) < AGENT_ANSWER.length;
  if (c.stream < c.ans.length) return true;
  if (c.follow && (c.followStream ?? 0) < c.follow.length) return true;
  if (c.drafting && (c.draftStream ?? 0) < ROOF_DRAFT.length) return true;
  return false;
}
export const viewing = (s: State, id: string) => s.mode === "bar" && s.winOpen && s.active === id;

export interface Presence { working: boolean; needs: boolean; unread: boolean; status: string; rank: number }
export function presence(c: Conv, s: State): Presence {
  const working = isWorking(c);
  const needs = !working && !!c.needs;
  const unread = !working && !needs && !!c.unread && !viewing(s, c.id);
  let status = "Done";
  if (working) status = c.forever ? "Watching" : "Working";
  else if (needs) status = "Needs you";
  else if (unread) status = "New result";
  else if (c.archived) status = "Done earlier";
  return { working, needs, unread, status, rank: needs ? 0 : working ? 1 : unread ? 2 : c.archived ? 4 : 3 };
}

function nameFrom(text: string) {
  const words = text.replace(/[^\w\s']/g, "").split(/\s+/).filter(Boolean).slice(0, 3).join(" ");
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : "New agent";
}
export const phrase = (s: State) => (s.listenTo && s.listenTo !== "gnsis" ? FOLLOW_PHRASE : QUESTION);

/** The open live turns for a conversation, shown while they are still being spoken. */
export const liveTurnsFor = (s: State, convId: string): Turn[] => (s.live && s.live.to === convId ? openTurns(s.live) : []);

export const liveInfoFor = (s: State): LiveInfo =>
  liveInfo(s.live, s.live ? s.convs[s.live.to]?.title ?? "GNSIS" : "", s.now);

/** Stand-in agents and canned replies are shown only in demo mode or when the host can take typed messages. */
const standIns = (s: State) => s.demo || s.caps.text;

// ---- setup ------------------------------------------------------------------
export function enterDesktop(identity: Identity, demo: boolean) {
  const convs: Record<string, Conv> = { gnsis: homeConv(identity.publicId, demo, demo || getState().caps.text) };
  let agentIds: string[] = [];
  let tabs = ["gnsis"];
  if (demo) {
    Object.assign(convs, demoAgents());
    agentIds = ["roof", "recipe", "watch", "triage", "gift"];
    tabs = ["gnsis", "roof", "recipe"];
  }
  setState({ phase: "desktop", identity, demo, convs, agentIds, tabs, active: "gnsis", mode: "dock", winOpen: false, greet: !demo, t: 0, live: null, toast: null });
}

export async function loadIdentity() {
  const store = identityStore;
  if (!store) return;
  try {
    const identity = await store.load();
    if (identity) enterDesktop(identity, getState().demo);
    else setState({ phase: "welcome" });
  } catch {
    setState({ phase: "welcome" });
  }
}

export async function createIdentity(): Promise<string | null> {
  const store = identityStore;
  if (!store || getState().creating) return null;
  setState({ creating: true });
  try {
    const [identity] = await Promise.all([store.create(), new Promise((r) => setTimeout(r, 1200))]);
    setState({ identity, creating: false, phase: "ready" });
    return null;
  } catch (e) {
    setState({ creating: false });
    return "Couldn’t create your key on this computer. " + plain(e);
  }
}

export async function eraseIdentity() {
  finishLive();
  await identityStore?.erase();
  setState({ settingsOpen: false, identity: null, phase: "welcome", live: null });
}

// ---- live voice -------------------------------------------------------------
function endLivePatch(s: State, note?: string): Partial<State> {
  if (!s.live) return {};
  const c = s.convs[s.live.to];
  const turns = endLiveTurns(s.live, Date.now(), note);
  return { live: null, convs: c ? { ...s.convs, [c.id]: { ...c, turns: [...c.turns, ...turns] } } : s.convs };
}

/** End live in the UI and tell the host. `note` is a plain sentence about why, if it was not the user's choice. */
function finishLive(note?: string) {
  const h = host;
  let was = false;
  setState((s) => {
    was = !!s.live;
    return s.live ? endLivePatch(s, note) : {};
  });
  if (was) void h?.endLive().catch(() => {});
}

const plain = (e: unknown) => (e instanceof Error ? e.message : typeof e === "string" ? e : "");

// ---- actions ----------------------------------------------------------------
export const actions = {
  openAgent(id: string) {
    const h = host;
    let endedLive = false;
    setState((s) => {
      const switching = !!s.live && s.live.to !== id;
      endedLive = switching;
      const base = switching ? { ...s, ...endLivePatch(s) } : s;
      // The chat being left was read up to now; its clock stops with the idle
      // ticks, so stamp it here rather than let it archive at once.
      const left = leaving(base);
      return {
        ...(switching ? endLivePatch(s) : {}),
        mode: "bar", dockMenu: false, visionMenu: false, active: id, winOpen: true, panelHidden: false, menuOpen: false, greet: false,
        toast: base.toast && base.toast.id === id ? null : base.toast,
        tabs: withTab(base.tabs, id),
        convs: withConv({ ...base, convs: left }, id, { unread: false, archived: false, readAt: base.t }),
      };
    });
    if (endedLive) void h?.endLive().catch(() => {});
  },
  toDock() {
    const h = host;
    let endedLive = false;
    setState((s) => {
      endedLive = !!s.live;
      const base = s.live ? { ...s, ...endLivePatch(s) } : s;
      return { ...(s.live ? endLivePatch(s) : {}), convs: leaving(base), mode: "dock", winOpen: false, listening: false, heard: 0, hold: 0, text: "", menuOpen: false, visionMenu: false, toast: null };
    });
    if (endedLive) void h?.endLive().catch(() => {});
  },
  closeTab(id: string) {
    const s = getState();
    if (id === "gnsis") {
      if (s.active === "gnsis") setState({ winOpen: false, menuOpen: false });
      return;
    }
    const i = s.tabs.indexOf(id);
    const tabs = s.tabs.filter((x) => x !== id);
    if (id !== s.active || !s.winOpen) return setState({ tabs });
    actions.openAgent(tabs[Math.min(i, tabs.length - 1)]);
    setState({ tabs });
  },
  startLive(to?: string) {
    const h = host;
    if (!h) return;
    const s0 = getState();
    if (!s0.caps.voice || s0.live) return;
    setState((s) => {
      const id = to && s.convs[to] ? to : "gnsis";
      return {
        mode: "bar", winOpen: true, active: id, panelHidden: false, tabs: withTab(s.tabs, id),
        convs: withConv(s, id, { unread: false, archived: false, readAt: s.t }),
        live: startLiveState(id, Date.now(), s.link), listening: false, heard: 0, hold: 0, now: Date.now(),
        dockMenu: false, visionMenu: false, greet: false, toast: null, menuOpen: false,
      };
    });
    h.startLive().catch((e) => finishLive("Live voice couldn’t start. " + (plain(e) || "The microphone did not open.")));
  },
  /** hardware gesture / voice button: live with whoever is in front, GNSIS from the dock */
  toggleLive() {
    const s = getState();
    if (s.phase !== "desktop") return;
    if (s.live) return actions.endLive();
    actions.startLive(s.mode === "bar" && s.winOpen ? s.active : "gnsis");
  },
  endLive() {
    finishLive();
  },
  toggleMute() {
    const h = host;
    let muted: boolean | null = null;
    setState((s) => {
      if (!s.live) return {};
      muted = !s.live.muted;
      return { live: { ...s.live, muted, userLevel: 0, userSpeaking: false } };
    });
    if (muted !== null) void h?.setMuted(muted).catch(() => {});
  },
  /** Point the visual sense at the screen or the camera, or switch it off. */
  setVision(source: VisionSource | null) {
    const h = host;
    if (!h) return;
    const caps = getState().caps;
    if (source && !caps[source]) return;
    if (visionTimer) clearTimeout(visionTimer);
    visionTimer = null;
    setState({ visionMenu: false, vision: { source, state: source ? "starting" : "off" } });
    if (source) {
      // "Starting" is a promise the runtime has to keep by accepting a frame.
      // If it never does, say so instead of showing a spinner for good.
      visionTimer = setTimeout(() => {
        visionTimer = null;
        setState((s) =>
          s.vision.state === "starting" && s.vision.source === source
            ? { vision: { source, state: "error", detail: "GNSIS hasn’t received a picture yet. The runtime may not be taking video." } }
            : {},
        );
      }, VISION_START_TIMEOUT_MS);
    }
    const p = source ? h.startVision(source) : h.stopVision();
    p.catch((e) => setState({ vision: { source, state: "error", detail: plain(e) || "The visual sense could not start." } }));
  },
  mic() {
    const s = getState();
    if (s.live || !s.caps.transcript) return;
    if (s.listening) return actions.finishListening();
    const to = s.mode === "bar" && s.winOpen && s.convs[s.active] ? s.active : "gnsis";
    setState({ mode: "bar", listening: true, heard: 0, hold: 0, text: "", dockMenu: false, winOpen: true, active: to, listenTo: to, toast: null, greet: false, tabs: withTab(s.tabs, to) });
  },
  typeInstead() {
    const s = getState();
    setState({ listening: false, heard: 0, hold: 0, text: phrase(s).split(" ").slice(0, s.heard).join(" ") });
  },
  finishListening() {
    const s = getState();
    if (s.listenTo === "gnsis") return actions.handoff(QUESTION, true, "focus");
    if (s.listenTo && s.convs[s.listenTo]) {
      const c = s.convs[s.listenTo];
      return actions.addTurn(c.id, FOLLOW_PHRASE, true, c.shortReply || "Got it.");
    }
    setState({ listening: false, heard: 0, hold: 0 });
  },
  addTurn(id: string, text: string, spoken: boolean, reply: string) {
    setState((s) => {
      const c = s.convs[id];
      const n: Conv = { ...c, stopped: false, needs: false, unread: false };
      if (c.kind === "agent") { n.agentT = AGENT_TOTAL; n.agentStream = AGENT_ANSWER.length; }
      else {
        n.stream = c.ans.length;
        if (c.follow) n.followStream = c.follow.length;
        if (c.drafting) n.draftStream = ROOF_DRAFT.length;
      }
      n.turns = [...c.turns, { role: "user", text, spoken }, { role: "agent", text: reply, stream: 0 }];
      return { convs: { ...s.convs, [id]: n }, listening: false, heard: 0, hold: 0, text: "", listenTo: null };
    });
  },
  /** GNSIS hands a job to a new agent and says so in its own chat (stand-in until agents are real) */
  handoff(text: string, spoken: boolean, fixedId: "focus" | null) {
    setState((s) => {
      let id: string;
      let conv: Conv;
      let seq = s.seq;
      if (fixedId === "focus") { id = "focus"; conv = focusConv(s.t); }
      else { seq += 1; id = "c" + seq; conv = spawnedConv(id, nameFrom(text), text, spoken, s.t); }
      const g = s.convs.gnsis;
      return {
        seq, listening: false, heard: 0, hold: 0, text: "", listenTo: null,
        convs: {
          ...s.convs, [id]: conv,
          gnsis: { ...g, turns: [...g.turns, { role: "user", text, spoken }, { role: "agent", text: "I started " + conv.title + " for this. It’s in the tab next to mine, and I’ll tell you when it’s done.", stream: 0 }] },
        },
        agentIds: s.agentIds.includes(id) ? s.agentIds : [...s.agentIds, id],
        tabs: withTab(s.tabs, id), mode: "bar", winOpen: true, active: "gnsis",
      };
    });
  },
  startAgent(req: string) {
    setState((s) => ({
      mode: "bar", listening: false, text: "", convs: { ...s.convs, recipe: recipeConv(req) },
      tabs: withTab(s.tabs, "recipe"), agentIds: s.agentIds.includes("recipe") ? s.agentIds : [...s.agentIds, "recipe"],
      active: "recipe", winOpen: true, panelHidden: false, menuOpen: false,
    }));
  },
  send() {
    const s = getState();
    const v = s.text.trim();
    if (!v) return;
    if (v.startsWith("/browse")) return actions.startAgent(v.slice(7).trim());
    if (v === "/screen" && s.caps.screen) {
      setState({ text: "" });
      return actions.setVision(s.vision.source === "screen" && s.vision.state !== "off" ? null : "screen");
    }
    if (v.startsWith("/") && !v.includes(" ")) {
      const first = filteredCommands(v)[0];
      if (first) setState({ text: first.name + " " });
      return;
    }
    const cur = s.winOpen ? s.convs[s.active] : null;
    if (!standIns(s)) {
      // The host cannot deliver typed words. Show what was typed, and say so —
      // never a made-up reply.
      const id = cur ? s.active : "gnsis";
      setState((st) => ({
        text: "",
        winOpen: true,
        convs: withConv(st, id, { turns: [...st.convs[id].turns, { role: "user", text: v }, { role: "system", text: TYPING_NOT_CONNECTED }] }),
      }));
      return;
    }
    if (!cur || s.active === "gnsis") return actions.handoff(v, false, null);
    if (s.active === "roof" && cur.approval === "pending") {
      setState({ apCustom: v, text: "" });
      return actions.answerApproval("custom");
    }
    actions.addTurn(s.active, v, false, "Got it. I’ll work that in.");
  },
  answerApproval(choice: number | "custom" | "skip") {
    setState((s) => {
      const c = s.convs.roof;
      if (!c || c.approval !== "pending") return {};
      let label: string;
      let follow: string;
      if (choice === "custom") { label = s.apCustom.trim() || "Something else"; follow = "Got it. I’ll work from that."; }
      else if (choice === "skip") { label = "Skipped"; follow = APPROVAL.follows[2]; }
      else { label = APPROVAL.options[choice]; follow = APPROVAL.follows[choice]; }
      return {
        apPick: null, apCustom: "",
        convs: withConv(s, "roof", { needs: false, approval: "answered", answeredLabel: label, follow, followStream: 0, drafting: choice === 0, draftStream: 0, panel: choice === 0 ? "email" : "list" }),
      };
    });
  },
  stop() {
    setState((s) => (s.convs[s.active] ? { convs: withConv(s, s.active, { stopped: true }) } : {}));
  },
  stopWatching() {
    setState((s) => ({ convs: withConv(s, s.active, { forever: false, readAt: s.t }) }));
  },
  toggleSteps() {
    setState((s) => {
      const c = s.convs[s.active];
      return c ? { convs: withConv(s, s.active, { stepsOpen: c.stepsOpen === false }) } : {};
    });
  },
  loadDemo() {
    const s = getState();
    if (s.identity) enterDesktop(s.identity, true);
  },
};

export function filteredCommands(text: string) {
  const q = text.slice(1).toLowerCase();
  return COMMANDS.filter((c) => c.name.slice(1).startsWith(q) || c.desc.toLowerCase().includes(q));
}

/** The chat being viewed, stamped as read now: called when the view moves away from it. */
function leaving(s: State): Record<string, Conv> {
  return viewing(s, s.active) && s.convs[s.active] ? withConv(s, s.active, { readAt: s.t }) : s.convs;
}

/**
 * Is anything on screen moving? A desktop app runs all day; when nothing is
 * streaming, speaking, listening, fading or about to leave the dock, the clock
 * stands still and nothing re-renders.
 */
export function isBusy(s: State): boolean {
  if (s.live || s.listening || s.toast) return true;
  for (const id of ["gnsis", ...s.agentIds]) {
    const c = s.convs[id];
    if (!c) continue;
    const lt = c.turns.length ? c.turns[c.turns.length - 1] : null;
    if (isWorking(c)) return true;
    if (lt && lt.role === "agent" && (lt.stream ?? 0) < lt.text.length && !c.stopped) return true;
    if (c.bornT !== undefined && s.t - c.bornT < 8) return true;
    // read and finished: it leaves the dock after a while, so keep counting
    if (id !== "gnsis" && !c.archived && !c.needs && !c.unread && c.readAt !== undefined && !viewing(s, id)) return true;
  }
  return false;
}

// ---- the clock --------------------------------------------------------------
// Drives the stand-in agents (streaming answers, badges, archiving) and, while
// live voice is on, the wall clock the status line and the bars read.
export function tick() {
  const s = getState();
  if (s.phase !== "desktop" || !isBusy(s)) return;
  const t = s.t + 1;
  const now = s.live ? Date.now() : s.now;
  if (s.listening) {
    const words = phrase(s).split(" ");
    const next: Partial<State> = { t, now };
    if (s.heard < words.length) {
      if (s.t % 4 === 0) next.heard = s.heard + 1;
    } else {
      next.hold = s.hold + 1;
      if (s.hold >= 12) {
        setState({ t, now });
        return actions.finishListening();
      }
    }
    return setState(next);
  }
  const convs = { ...s.convs };
  let toast = s.toast ? { ...s.toast, ttl: s.toast.ttl - 1 } : null;
  if (toast && toast.ttl <= 0) toast = null;
  for (const id of ["gnsis", ...s.agentIds]) {
    let c = convs[id];
    if (!c) continue;
    const isViewing = viewing(s, id);
    const lt = c.turns.length ? c.turns[c.turns.length - 1] : null;
    // A live reply is committed whole; only stand-in replies stream by ticks.
    if (lt && lt.role === "agent" && (lt.stream ?? 0) < lt.text.length && !c.stopped) {
      const turns = c.turns.slice();
      turns[turns.length - 1] = { ...lt, stream: Math.min(lt.text.length, (lt.stream ?? 0) + 3) };
      const n = { ...c, turns };
      if (!isWorking(n) && !isViewing) {
        n.unread = true;
        if (s.mode === "bar") toast = { id, text: "Replied to you", ttl: 80 };
      }
      c = n;
    } else if (isWorking(c) && !c.forever) {
      const n = { ...c };
      if (c.kind === "agent") {
        if ((c.agentT ?? 0) < AGENT_TOTAL) n.agentT = (c.agentT ?? 0) + 1;
        else n.agentStream = Math.min(AGENT_ANSWER.length, (c.agentStream ?? 0) + 3);
      } else if (c.stream < c.ans.length) n.stream = Math.min(c.ans.length, c.stream + 3);
      else if (c.follow && (c.followStream ?? 0) < c.follow.length) n.followStream = Math.min(c.follow.length, (c.followStream ?? 0) + 3);
      else if (c.drafting) n.draftStream = Math.min(ROOF_DRAFT.length, (c.draftStream ?? 0) + 5);
      if (!isWorking(n) && !isViewing) {
        n.unread = true;
        if (s.mode === "bar") toast = { id, text: n.doneText || "Finished", ttl: 80 };
      }
      c = n;
    }
    if (isViewing) c = { ...c, unread: false, archived: false, readAt: t };
    else if (id !== "gnsis" && !isWorking(c) && !c.needs && !c.unread && !c.archived && c.readAt !== undefined && t - c.readAt > 140) c = { ...c, archived: true };
    convs[id] = c;
  }
  setState({ t, now, convs, toast });
}
