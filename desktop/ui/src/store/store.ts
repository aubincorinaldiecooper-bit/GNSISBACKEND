import { useSyncExternalStore } from "react";
import {
  AGENT_ANSWER, AGENT_TOTAL, APPROVAL, COMMANDS, FOLLOW_PHRASE, QUESTION, ROOF_DRAFT, TYPING_NOT_CONNECTED,
  demoAgents, focusConv, homeConv, recipeConv, spawnedConv,
  type Conv, type StepState, type Turn,
} from "../demo/data";
import type { HostCapabilities, Identity, IdentityStore, LinkState, LiveEvent, LiveHost, VisionSource } from "../host";
import { actionLine, applyLiveEvent, cutText, endLiveTurns, liveInfo, liveThinking, openTurns, startLiveState, type LiveInfo, type LiveState } from "./live";
import { greetedBefore, markGreeted } from "../lib/place";
import { menuBarGlyph } from "../lib/menuBarFace";

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
  /**
   * The typed message GNSIS owes an answer: `sent` until the runtime accepts
   * it, then `accepted` until GNSIS's reply to it ends, the wait runs out, or
   * the connection closes. What GNSIS does meanwhile keeps it open and starts
   * the wait over. `seq` tells one message from the next.
   */
  awaiting: { to: string; seq: number; since: number; state: "sent" | "accepted" } | null;
  /** GNSIS's reply to a typed message, while its words are still arriving (outside live voice). */
  reply: { to: string; text: string } | null;
  /** What GNSIS is doing on this computer right now, until it reports how it went. */
  working: { to: string; text: string; since?: number } | null;
  /** An action GNSIS is waiting for the person to allow, until it runs or is refused. */
  asking: { to: string; text: string } | null;
  /** The rest of a reply the person cut off by ending live voice: not shown when it arrives. */
  dropTail: boolean;
  /** Some runtime in this session offered typed turns: when typing is off now, the link is what is missing. */
  typingOffered: boolean;
  /** the runtime link as last reported by the host, live or not */
  link: LinkState;
  vision: VisionState;
  greet: boolean;
  /** GNSIS is tucked into its menu bar icon; `tuckAt` is where that icon is, so it shrinks toward it. */
  tucked: boolean;
  tuckAt: { x: number; y: number } | null;
  /** show the sample agents so every state can be reviewed */
  demo: boolean;
}

const NO_CAPS: HostCapabilities = { voice: false, text: false, screen: false, camera: false, transcript: false, overlay: false, menuBar: false };

/** How long the visual sense may sit on "starting" before that is reported as a problem. */
const VISION_START_TIMEOUT_MS = 20_000;
/** How long a typed message may go unanswered before the chat says so. */
export const REPLY_TIMEOUT_MS = 60_000;
/** Said when GNSIS accepted a typed message but neither answered nor acted on it in time. */
export const NO_ANSWER_YET = "GNSIS hasn’t answered that yet. It may still be working; you can also try asking with your voice.";
/** Said when the connection closes while a typed message is still owed an answer. */
export const CLOSED_BEFORE_ANSWER = "The connection to GNSIS closed before it answered.";
/** Typing that cannot be delivered because the connection is down, on a runtime that does take typing. */
export const TYPING_LINK_DOWN = "Not sent. GNSIS isn’t connected right now. Try again in a moment.";
/** Typing that cannot be delivered during live voice: the voice button would end the call, so do not point at it. */
export const TYPING_WHILE_LIVE = "Typing isn’t connected to GNSIS yet. You’re live, so just say it.";
export const TYPING_WHILE_MUTED = "Typing isn’t connected to GNSIS yet. You’re muted: unmute and say it.";
export const TYPING_WHILE_CONNECTING = "Typing isn’t connected to GNSIS yet. It’s still connecting; say it once the bar shows Listening.";

const initial: State = {
  phase: "loading", creating: false, identity: null, caps: NO_CAPS,
  mode: "dock", convs: {}, agentIds: [], tabs: ["gnsis"], active: "gnsis", winOpen: false,
  listening: false, listenTo: null, heard: 0, hold: 0, text: "", t: 0, now: Date.now(), seq: 0,
  panelHidden: true, menuOpen: false, dockMenu: false, visionMenu: false, settingsOpen: false,
  toast: null, apPick: null, apCustom: "", live: null, awaiting: null, reply: null, working: null, asking: null,
  dropTail: false, typingOffered: false, link: "connecting",
  vision: { source: null, state: "off" }, greet: false, tucked: false, tuckAt: null, demo: false,
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
  return useSyncExternalStore(subscribe, () => select(state), () => select(state));
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
/** How long GNSIS takes to shrink into the menu bar icon (styles.css, `.desktop.is-tucked`). */
export const TUCK_MS = 300;
/** Bumped by every tuck and return, so a late hide never hides GNSIS after it came back. */
let tuckTurn = 0;
/** Bumped by every choice of what GNSIS sees, so a late answer to an older choice changes nothing. */
let visionAttempt = 0;

function onLiveEvent(ev: LiveEvent) {
  if (ev.type === "menubar") {
    if (ev.want === "hide") actions.tuckAway();
    else actions.comeBack();
    return;
  }
  if (ev.type === "vision") {
    if (ev.state !== "starting" && visionTimer) {
      clearTimeout(visionTimer);
      visionTimer = null;
    }
    setState({ vision: { source: ev.source, state: ev.state, detail: ev.detail } });
    if ((ev.state === "denied" || ev.state === "error") && ev.detail) visionTrouble(ev.detail);
    return;
  }
  // What the host can do may change with the runtime it is connected to
  // (typing needs a runtime that answers typed turns), so read it again.
  if (ev.type === "link") {
    const caps = host ? host.capabilities() : getState().caps;
    setState((s) => ({ link: ev.state, caps, typingOffered: s.typingOffered || caps.text }));
  }
  if (!getState().live) {
    onIdleEvent(ev);
  } else {
    let ended: string | undefined;
    setState((s) => {
      if (!s.live) return {};
      const step = applyLiveEvent(s.live, ev, Date.now());
      const c = s.convs[s.live.to];
      if (step.ended) ended = step.ended.reason;
      // Only GNSIS's own reply answers a typed message; the person speaking,
      // or a line about an action, leaves it open.
      const answered = step.commit.some((t) => t.role === "agent") && s.awaiting?.to === s.live.to ? { awaiting: null } : {};
      // News for a chat closed during the call is marked, as it is outside live.
      // A step that ended started when its "working" did.
      step.commit = step.commit.map((t) => (t.step && s.working ? { ...t, step: { ...t.step, startedAt: s.working.since } } : t));
      const news = step.commit.filter((t) => t.role !== "user");
      const landed = step.commit.length && c
        ? news.length && !viewing(s, c.id)
          ? land(s, c.id, step.commit, news.at(-1)!.role === "agent" ? "Replied to you" : news.at(-1)!.text)
          : { convs: { ...s.convs, [c.id]: { ...c, turns: [...c.turns, ...step.commit] } } }
        : {};
      return { live: step.live, ...landed, ...answered };
    });
    if (ended !== undefined) finishLive(ended);
  }
  if (ev.type === "action") noteAction(ev);
  if (ev.type === "link" && (ev.state === "closed" || ev.state === "error")) linkLost();
}

/**
 * The moment an action starts is shown while it runs, and cleared by its
 * outcome — which lands in the chat as a line, live or not. One waiting for
 * the person's OK is kept until it runs or is refused, so the Activity button
 * can show it.
 */
function noteAction(ev: Extract<LiveEvent, { type: "action" }>) {
  setState((s) => {
    const to = s.live?.to ?? s.awaiting?.to ?? s.reply?.to ?? "gnsis";
    const text = ev.text.trim();
    const working = ev.state === "working" ? (text ? { to, text, since: Date.now() } : s.working) : null;
    const asking = ev.state === "waiting" && text ? { to, text } : null;
    return working === s.working && asking === s.asking ? {} : { working, asking };
  });
}

/**
 * The connection closed outside live voice (live says so itself as it ends):
 * a reply still arriving stops where it is, and a typed message still owed an
 * answer is told plainly that none is coming on this connection.
 */
function linkLost() {
  setState((s) => {
    if (s.live || (!s.reply && !s.awaiting)) return {};
    let convs = flushReply(s, true);
    const c = s.awaiting ? convs[s.awaiting.to] : undefined;
    if (c) convs = { ...convs, [c.id]: { ...c, turns: [...c.turns, { role: "system", text: CLOSED_BEFORE_ANSWER }] } };
    return { convs, reply: null, awaiting: null };
  });
}

/** A reply still arriving, kept as it stands: ended with a dash when it was cut off. */
function flushReply(s: State, cut: boolean): Record<string, Conv> {
  const r = s.reply;
  const c = r ? s.convs[r.to] : undefined;
  if (!r || !c || !r.text.trim()) return s.convs;
  const text = cut ? cutText(r.text) : r.text;
  return { ...s.convs, [c.id]: { ...c, turns: [...c.turns, { role: "agent", text, stream: text.length }] } };
}

/**
 * News for a chat. When the person is not looking at it, the chat is marked
 * unread, and in the bar a toast says what came in.
 */
function land(s: State, to: string, turns: Turn[], news: string): Partial<State> {
  const c = s.convs[to];
  if (!c) return {};
  const seen = viewing(s, to);
  return {
    convs: withConv(s, to, { turns: [...c.turns, ...turns], ...(seen ? {} : { unread: true }) }),
    ...(seen || s.mode !== "bar" ? {} : { toast: { id: to, text: news, ttl: 80 } }),
  };
}

/** Why the screen or camera is not being seen, said where the person will read it. */
function visionTrouble(detail: string) {
  setState((s) => {
    const to = s.mode === "bar" && s.winOpen && s.convs[s.active] ? s.active : "gnsis";
    const last = s.convs[to]?.turns.at(-1);
    if (last?.role === "system" && last.text === detail) return {};
    return land(s, to, [{ role: "system", text: detail }], detail);
  });
}

/**
 * Outside live voice, what GNSIS does on this computer still belongs in the
 * chat — an action the person approved after ending the call, or one they
 * asked for by typing — and so do its words. The one exception is the rest of
 * a reply the person cut off by ending live voice, which is not shown.
 */
function onIdleEvent(ev: LiveEvent) {
  if (ev.type !== "action" && ev.type !== "agent.text") return;
  setState((s) => {
    if (ev.type === "action") {
      const line = actionLine(ev.state, ev.text);
      const to = s.awaiting?.to ?? s.reply?.to ?? s.working?.to ?? "gnsis";
      if (!s.convs[to]) return {};
      // A new step comes after what GNSIS has said so far: those words close
      // first, so the step never lands above them.
      const starts = !s.working && !s.asking;
      const said: Turn[] = starts && s.reply?.to === to && s.reply.text.trim() ? [{ role: "agent", text: s.reply.text, stream: s.reply.text.length }] : [];
      if (!line) return said.length ? { ...land(s, to, said, "Replied to you"), reply: null } : {};
      // GNSIS is still on it: a typed message stays open, and its wait starts over.
      const step = { state: ev.state as StepState, startedAt: s.working?.since, endedAt: Date.now() };
      return {
        ...land(s, to, [...said, { role: "system", text: line, step }], line),
        ...(said.length ? { reply: null } : {}),
        awaiting: s.awaiting ? { ...s.awaiting, since: Date.now() } : null,
      };
    }
    if (s.dropTail) return ev.endOfTurn || ev.interrupted ? { dropTail: false } : {};
    const to = s.reply?.to ?? s.awaiting?.to ?? "gnsis";
    if (!s.convs[to]) return {};
    let text = (s.reply?.to === to ? s.reply.text : "") + ev.text;
    const done: Turn[] = [];
    if (ev.interrupted) {
      if (text.trim()) done.push({ role: "agent", text: cutText(text), stream: cutText(text).length });
      text = "";
    } else if (ev.endOfTurn) {
      if (text.trim()) done.push({ role: "agent", text, stream: text.length });
      text = "";
    }
    return {
      reply: text ? { to, text } : null,
      // A reply that has ended is the answer; an empty end is not.
      awaiting: done.length ? null : s.awaiting,
      ...(done.length ? land(s, to, done, "Replied to you") : {}),
    };
  });
}

// ---- helpers ----------------------------------------------------------------
/**
 * The drawer when `id` becomes the chat in front: an agent's chat opens with
 * its work beside it; GNSIS's own chat never opens the drawer, and coming
 * back to it from an agent's chat closes it again. Used wherever the chat in
 * front changes.
 */
function panelFor(s: State, id: string): boolean {
  if (id !== "gnsis" && s.convs[id]?.panel) return false;
  return s.active !== id && s.active !== "gnsis" ? true : s.panelHidden;
}
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

/**
 * What the Activity button shows: how many things need the person, are
 * working, or have news. GNSIS's own actions on this computer count too: one
 * waiting for the person's OK needs them, one running is working.
 */
export function activity(s: State): { needs: number; working: number; unread: number } {
  const out = { needs: s.asking ? 1 : 0, working: s.working ? 1 : 0, unread: 0 };
  for (const id of s.agentIds) {
    const c = s.convs[id];
    if (!c) continue;
    const p = presence(c, s);
    if (p.needs) out.needs += 1;
    else if (p.working) out.working += 1;
    else if (p.unread) out.unread += 1;
  }
  return out;
}

function nameFrom(text: string) {
  const words = text.replace(/[^\w\s']/g, "").split(/\s+/).filter(Boolean).slice(0, 3).join(" ");
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : "New agent";
}
export const phrase = (s: State) => (s.listenTo && s.listenTo !== "gnsis" ? FOLLOW_PHRASE : QUESTION);

/**
 * Whether a chat has anything to show beyond GNSIS's opening line. Until it
 * does, the bar is shown on its own, with no chat window above it. A spoken
 * turn without words shows nothing, so on its own it does not count either.
 */
export function hasConversation(s: State, id: string): boolean {
  const c = s.convs[id];
  if (!c) return false;
  if (c.kind !== "home") return true;
  const shown = (t: Turn) => !t.greeting && !(t.role === "user" && t.spoken && !t.text);
  return (
    c.turns.some(shown) ||
    liveTurnsFor(s, id).some(shown) ||
    s.awaiting?.to === id ||
    s.working?.to === id ||
    (s.live?.to === id && liveThinking(s.live, s.now)) ||
    (s.listening && s.listenTo === id)
  );
}

/** The open turns for a conversation: live ones while spoken, or a typed message's reply while it arrives. */
export const liveTurnsFor = (s: State, convId: string): Turn[] => {
  if (s.live && s.live.to === convId) return openTurns(s.live);
  if (s.reply && s.reply.to === convId) return [{ role: "agent", text: s.reply.text, stream: s.reply.text.length, speaking: true }];
  return [];
};

export const liveInfoFor = (s: State): LiveInfo =>
  liveInfo(s.live, s.live ? s.convs[s.live.to]?.title ?? "GNSIS" : "", s.now);

/**
 * Stand-in agents and canned replies are for demo mode only. Everywhere else
 * every reply, agent and result comes from the runtime or the host.
 */
const standIns = (s: State) => s.demo;

// ---- setup ------------------------------------------------------------------
export function enterDesktop(identity: Identity, demo: boolean) {
  // The greeting is for the first launch on this computer only.
  const greet = !demo && !greetedBefore();
  if (greet) markGreeted();
  const convs: Record<string, Conv> = { gnsis: homeConv(identity.publicId, demo, demo || getState().caps.text) };
  let agentIds: string[] = [];
  let tabs = ["gnsis"];
  if (demo) {
    Object.assign(convs, demoAgents());
    agentIds = ["roof", "recipe", "watch", "triage", "gift"];
    tabs = ["gnsis", "roof", "recipe"];
  }
  // The menu bar icon is this GNSIS's own face.
  if (getState().caps.menuBar) host?.menuBarFace?.(menuBarGlyph(identity.publicId));
  setState({
    phase: "desktop", identity, demo, convs, agentIds, tabs, active: "gnsis", mode: "dock", winOpen: false, greet, t: 0, tucked: false,
    live: null, toast: null, awaiting: null, reply: null, working: null, asking: null, dropTail: false,
  });
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

/**
 * Erase this computer's GNSIS. Everything it was doing stops first — live
 * voice and the screen or camera it was seeing — so nothing keeps running
 * behind the welcome screen. Returns why, if the key could not be erased.
 */
export async function eraseIdentity(): Promise<string | null> {
  finishLive();
  if (visionTimer) {
    clearTimeout(visionTimer);
    visionTimer = null;
  }
  if (getState().vision.state !== "off") await host?.stopVision().catch(() => {});
  try {
    await identityStore?.erase();
  } catch (e) {
    return "Couldn’t erase your key from this computer. " + (plain(e) || "Try again.");
  }
  setState({
    settingsOpen: false, identity: null, phase: "welcome", live: null, vision: { source: null, state: "off" },
    awaiting: null, reply: null, working: null, asking: null,
  });
  return null;
}

// ---- live voice -------------------------------------------------------------
function endLivePatch(s: State, note?: string): Partial<State> {
  if (!s.live) return {};
  const c = s.convs[s.live.to];
  const turns = endLiveTurns(s.live, Date.now(), note);
  return {
    live: null,
    // Words the runtime still sends for the call just ended are its tail, not
    // news: not shown — unless a typed message is still owed its answer.
    dropTail: !s.awaiting,
    convs: c ? { ...s.convs, [c.id]: { ...c, turns: [...c.turns, ...turns] } } : s.convs,
  };
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
  /** The person closed the first-launch greeting. */
  closeGreeting() {
    setState({ greet: false });
  },
  /**
   * Tuck GNSIS into its menu bar icon (the owner's choices, 30 September):
   * a call in progress ends, and once GNSIS has shrunk into the icon the host
   * hides it until the icon is clicked again.
   */
  tuckAway() {
    const s = getState();
    if (s.phase !== "desktop" || s.tucked) return;
    if (s.live) actions.endLive();
    const turn = ++tuckTurn;
    setState({ tucked: true, tuckAt: host?.menuBarIcon?.() ?? null, dockMenu: false, visionMenu: false });
    setTimeout(() => {
      if (turn === tuckTurn && getState().tucked) host?.hideToMenuBar?.();
    }, TUCK_MS);
  },
  /** The menu bar icon was clicked again: GNSIS comes back where it was. */
  comeBack() {
    if (!getState().tucked) return;
    tuckTurn++;
    setState({ tucked: false, tuckAt: host?.menuBarIcon?.() ?? getState().tuckAt });
  },
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
      const panelHidden = panelFor(s, id);
      return {
        ...(switching ? endLivePatch(s) : {}),
        panelHidden,
        mode: "bar", dockMenu: false, visionMenu: false, active: id, winOpen: true, menuOpen: false, greet: false,
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
      // A typed message's reply still arriving carries on as the live reply in
      // the same chat; in another chat it is kept as it stands.
      const carry = s.reply?.to === id ? s.reply.text : null;
      const base = { ...s, convs: carry ? s.convs : flushReply(s, false) };
      const live = startLiveState(id, Date.now(), s.link);
      return {
        mode: "bar", winOpen: true, active: id, tabs: withTab(s.tabs, id), panelHidden: panelFor(s, id),
        convs: withConv(base, id, { unread: false, archived: false, readAt: s.t }),
        live: carry ? { ...live, agent: { text: carry } } : live, reply: null, dropTail: false,
        listening: false, heard: 0, hold: 0, now: Date.now(),
        dockMenu: false, visionMenu: false, greet: false, toast: null, menuOpen: false,
      };
    });
    h.startLive().catch((e) => finishLive("Live voice couldn’t start. " + (plain(e) || "The microphone did not open.")));
  },
  /**
   * The Activity drawer: what GNSIS's agents are doing, opened only when the
   * person wants to look. Nothing opens it on its own — an agent that needs
   * the person marks the Activity button instead.
   */
  toggleActivity() {
    setState((s) => ({ panelHidden: !s.panelHidden, menuOpen: false }));
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
    const attempt = ++visionAttempt;
    setState({ visionMenu: false, vision: { source, state: source ? "starting" : "off" } });
    const p = source ? h.startVision(source) : h.stopVision();
    p.then(
      () => {
        // "Starting" is a promise the runtime has to keep by accepting a
        // frame. The clock starts once sharing has really begun — time spent
        // in the system picker or a permission prompt does not count — and if
        // no picture is accepted in time, the capture is stopped too, so
        // "not seeing" on screen really means off.
        if (!source || attempt !== visionAttempt) return;
        visionTimer = setTimeout(() => {
          visionTimer = null;
          const now = getState().vision;
          if (attempt !== visionAttempt || now.state !== "starting" || now.source !== source) return;
          const detail = `GNSIS hadn’t received a picture after ${VISION_START_TIMEOUT_MS / 1000} seconds, so sharing your ${source} was stopped. The runtime may not be taking video.`;
          void h.stopVision().catch(() => {}).finally(() => {
            setState({ vision: { source, state: "error", detail } });
            visionTrouble(detail);
          });
        }, VISION_START_TIMEOUT_MS);
      },
      (e) => {
        if (attempt !== visionAttempt) return;
        const detail = plain(e) || "The visual sense could not start.";
        setState({ vision: { source, state: "error", detail } });
        visionTrouble(detail);
      },
    );
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
        tabs: withTab(s.tabs, id), mode: "bar", winOpen: true, active: "gnsis", panelHidden: panelFor(s, "gnsis"),
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
    if (v.startsWith("/browse") && s.demo) return actions.startAgent(v.slice(7).trim());
    if (v === "/screen" && s.caps.screen) {
      setState({ text: "" });
      return actions.setVision(s.vision.source === "screen" && s.vision.state !== "off" ? null : "screen");
    }
    if (v.startsWith("/") && !v.includes(" ")) {
      const first = filteredCommands(v, s)[0];
      if (first) setState({ text: first.name + " " });
      return;
    }
    const cur = s.winOpen ? s.convs[s.active] : null;
    if (!standIns(s)) {
      const id = cur ? s.active : "gnsis";
      if (!s.caps.text || !host?.sendText) {
        // The host cannot deliver typed words. Show what was typed, and say so —
        // never a made-up reply.
        const why = notConnected(s);
        setState((st) => ({
          text: "",
          mode: "bar",
          winOpen: true,
          active: id,
          panelHidden: panelFor(st, id),
          // The chat is in view now: whatever was new in it has been seen.
          convs: withConv(st, id, { unread: false, archived: false, turns: [...st.convs[id].turns, { role: "user", text: v }, { role: "system", text: why }] }),
        }));
        return;
      }
      return sendTyped(id, v);
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
  /** Refill the demo's sample agents. Only in demo mode: the real app has no stand-ins to load. */
  loadDemo() {
    const s = getState();
    if (s.identity && s.demo) enterDesktop(s.identity, true);
  },
};

/**
 * The commands that do something here: every one in demo mode, and only the
 * real ones otherwise — a command that would only start a stand-in agent is
 * not offered in a real build.
 */
export function available(s: Pick<State, "demo" | "caps">) {
  return COMMANDS.filter((c) => s.demo || (c.name === "/screen" && s.caps.screen));
}

export function filteredCommands(text: string, s: Pick<State, "demo" | "caps"> = getState()) {
  const q = text.slice(1).toLowerCase();
  return available(s).filter((c) => c.name.slice(1).startsWith(q) || c.desc.toLowerCase().includes(q));
}

/** Why a typed message cannot go, said for the moment the person is in. */
function notConnected(s: State): string {
  if (s.typingOffered && s.link !== "ready") return TYPING_LINK_DOWN;
  if (s.live) return s.live.phase === "connecting" ? TYPING_WHILE_CONNECTING : s.live.muted ? TYPING_WHILE_MUTED : TYPING_WHILE_LIVE;
  return TYPING_NOT_CONNECTED;
}

/**
 * A typed message, sent to GNSIS as the person's own turn. It shows at once;
 * then "Sending…" until the runtime accepts it, and "working" until GNSIS
 * answers. If it could not be sent, the chat says why. Each message keeps its
 * own place in line: what happens to one never changes what the chat says
 * about another.
 */
function sendTyped(id: string, text: string) {
  const h = host;
  if (!h?.sendText) return;
  let seq = 0;
  setState((st) => {
    seq = st.seq + 1;
    // A reply still arriving is kept as it stands; whatever follows is a new one.
    const convs = flushReply(st, false);
    return {
      seq,
      text: "",
      mode: "bar",
      winOpen: true,
      active: id,
      greet: false,
      reply: null,
      dropTail: false,
      awaiting: { to: id, seq, since: Date.now(), state: "sent" },
      panelHidden: panelFor(st, id),
      convs: withConv({ ...st, convs }, id, { unread: false, archived: false, turns: [...convs[id].turns, { role: "user", text }] }),
    };
  });
  h.sendText(text).then(
    // The wait for an answer starts when the runtime has the message.
    () => setState((st) => (st.awaiting?.seq === seq && st.awaiting.state === "sent" ? { awaiting: { ...st.awaiting, state: "accepted", since: Date.now() } } : {})),
    (e: unknown) =>
      setState((st) => ({
        awaiting: st.awaiting?.seq === seq ? null : st.awaiting,
        convs: st.convs[id] ? withConv(st, id, { turns: [...st.convs[id].turns, { role: "system", text: notSent(e) }] }) : st.convs,
      })),
  );
}

/**
 * A message that went out but was never confirmed is not called "not sent":
 * the runtime may have it. The host marks that case `unconfirmed`.
 */
function notSent(e: unknown): string {
  const why = plain(e) || "The message could not reach GNSIS.";
  return (e as { unconfirmed?: boolean } | null)?.unconfirmed ? why : "Not sent. " + why;
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
  if (s.live || s.listening || s.toast || s.awaiting || s.reply || s.working || s.asking) return true;
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
// live voice is on or a typed message waits, the wall clock the status line,
// the bars and "Thinking" read.
export function tick() {
  const s = getState();
  if (s.phase !== "desktop" || !isBusy(s)) return;
  const t = s.t + 1;
  const now = s.live || s.awaiting ? Date.now() : s.now;
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
  let awaiting = s.awaiting;
  // The wait counts from when the runtime had the message, or from GNSIS's
  // last step on it; it does not run while an action is under way, waiting
  // for the person, or while the reply is arriving.
  const underWay = !!s.reply || (!!s.working && s.working.to === awaiting?.to) || !!s.asking;
  if (awaiting && awaiting.state === "accepted" && !underWay && Date.now() - awaiting.since > REPLY_TIMEOUT_MS && convs[awaiting.to]) {
    // Accepted, but nothing came back: say so rather than wait in silence.
    const c = convs[awaiting.to];
    convs[awaiting.to] = { ...c, turns: [...c.turns, { role: "system", text: NO_ANSWER_YET }] };
    awaiting = null;
  }
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
  setState({ t, now, convs, toast, awaiting });
}
