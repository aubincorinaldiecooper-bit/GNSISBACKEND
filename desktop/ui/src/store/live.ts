/**
 * Live voice as the chat sees it.
 *
 * Pure functions: a live-session state, an event from the host, and the
 * turns that become part of the conversation because of it. Nothing here
 * knows about React or about the host. The store applies these; tests drive
 * them directly.
 */

import type { LinkState, LiveEvent } from "../host";
import type { Turn } from "../demo/data";
import { clock } from "../lib/platform";

/**
 * What the session is really doing, from device truth, never from intent:
 * `connecting` until the host reports the microphone on, `responding` while a
 * reply is actually playing, `listening` otherwise.
 */
export type LivePhase = "connecting" | "listening" | "responding";

export interface LiveState {
  /** The conversation the words land in. */
  to: string;
  startedAt: number;
  phase: LivePhase;
  /** When the microphone really came on. The clock counts from here, not from the button press. */
  listeningSince: number | null;
  muted: boolean;
  link: LinkState;
  linkDetail?: string;
  /** The reply being spoken right now: its words so far. */
  agent: { text: string } | null;
  /** The person's turn in progress: since when, and its words if the host has them. */
  user: { startedAt: number; text: string } | null;
  /** Device playback truth, not "the model produced audio". */
  agentSpeaking: boolean;
  userSpeaking: boolean;
  agentLevel: number;
  userLevel: number;
}

export interface LiveStep {
  live: LiveState;
  /** Turns that became final because of this event, in order. */
  commit: Turn[];
  /** The session cannot continue (link lost, microphone refused). */
  ended?: { reason: string };
}

export function startLiveState(to: string, now: number, link: LinkState): LiveState {
  return {
    to,
    startedAt: now,
    phase: "connecting",
    listeningSince: null,
    muted: false,
    link,
    agent: null,
    user: null,
    agentSpeaking: false,
    userSpeaking: false,
    agentLevel: 0,
    userLevel: 0,
  };
}

/** The sentence was cut off: drop the half word and end on a dash. */
export function cutText(text: string): string {
  const trimmed = text.replace(/\s+\S*$/, "");
  return (trimmed || text.trimEnd()) + "—";
}

const agentTurn = (text: string): Turn => ({ role: "agent", text, stream: text.length });

export function applyLiveEvent(live: LiveState, ev: LiveEvent, now: number): LiveStep {
  const commit: Turn[] = [];
  let next: LiveState = live;

  switch (ev.type) {
    case "link": {
      next = { ...live, link: ev.state, linkDetail: ev.detail };
      if (ev.state === "error" || ev.state === "closed") {
        return { live: next, commit, ended: { reason: ev.detail || (ev.state === "error" ? "The connection failed." : "The session ended.") } };
      }
      break;
    }
    case "agent.text": {
      let text = (live.agent?.text ?? "") + ev.text;
      if (ev.interrupted) {
        if (text.trim()) commit.push(agentTurn(cutText(text)));
        text = "";
      } else if (ev.endOfTurn) {
        if (text.trim()) commit.push(agentTurn(text));
        text = "";
      }
      next = { ...live, agent: text ? { text } : null };
      break;
    }
    case "agent.cut": {
      if (live.agent?.text.trim()) commit.push(agentTurn(cutText(live.agent.text)));
      next = { ...live, agent: null, agentSpeaking: false, agentLevel: 0, phase: settle(live.phase, false) };
      break;
    }
    case "agent.speaking":
      next = {
        ...live,
        agentSpeaking: ev.speaking,
        agentLevel: ev.speaking ? live.agentLevel : 0,
        phase: settle(live.phase, ev.speaking),
      };
      break;
    case "agent.level":
      next = { ...live, agentLevel: clamp(ev.level) };
      break;
    case "user.level":
      next = { ...live, userLevel: live.muted ? 0 : clamp(ev.level) };
      break;
    case "user.speech": {
      if (ev.state === "start") {
        next = { ...live, userSpeaking: true, user: live.user ?? { startedAt: now, text: "" } };
      } else {
        if (live.user) {
          commit.push({
            role: "user",
            text: live.user.text,
            spoken: true,
            spokenMs: ev.ms ?? Math.max(0, now - live.user.startedAt),
          });
        }
        next = { ...live, userSpeaking: false, user: null, userLevel: 0 };
      }
      break;
    }
    case "user.words": {
      if (live.user) next = { ...live, user: { ...live.user, text: ev.text } };
      else if (ev.final && ev.text.trim()) commit.push({ role: "user", text: ev.text, spoken: true });
      break;
    }
    case "mic": {
      if (ev.state === "denied" || ev.state === "error") {
        return {
          live,
          commit,
          ended: { reason: ev.detail || (ev.state === "denied" ? "The microphone was not allowed." : "The microphone stopped.") },
        };
      }
      // The one event that means the microphone is really capturing: only
      // now does the session listen, and only now does its clock start.
      if (ev.state === "on" && live.phase === "connecting") {
        next = { ...live, phase: live.agentSpeaking ? "responding" : "listening", listeningSince: now };
      }
      break;
    }
    case "action": {
      // What GNSIS did on the computer goes in the conversation as a plain
      // line; the moment it starts does not, only what came of it.
      const line = actionLine(ev.state, ev.text);
      if (line) commit.push({ role: "system", text: line });
      break;
    }
    case "vision":
      // Not part of a live session; the store keeps it separately.
      break;
  }
  return { live: next, commit };
}

/** The line an action's outcome leaves in the chat; its start leaves none. */
export function actionLine(state: string, text: string): string | null {
  const said = text.trim();
  if (!said) return null;
  switch (state) {
    case "waiting":
    case "declined":
    case "needs_permission":
      return said;
    case "done":
      return `Done: ${said}`;
    case "failed":
      return `Couldn’t do it: ${said}`;
    default:
      return null;
  }
}

/** A reply playing makes the session respond; anything else leaves it listening, once it is listening at all. */
function settle(phase: LivePhase, speaking: boolean): LivePhase {
  if (phase === "connecting") return phase;
  return speaking ? "responding" : "listening";
}

/**
 * The turns still open when live ends, plus the closing line. A session that
 * never got its microphone was never a conversation: it leaves no duration
 * line, only the reason it stopped, if it was not the person's choice.
 */
export function endLiveTurns(live: LiveState, now: number, note?: string): Turn[] {
  const out: Turn[] = [];
  if (live.agent?.text.trim()) out.push(agentTurn(live.agentSpeaking ? cutText(live.agent.text) : live.agent.text));
  if (live.user) out.push({ role: "user", text: live.user.text, spoken: true, spokenMs: Math.max(0, now - live.user.startedAt) });
  if (live.listeningSince !== null) {
    out.push({ role: "system", text: `Live conversation · ${clock(Math.max(1000, now - live.listeningSince))}` });
  }
  if (note) out.push({ role: "system", text: note });
  return out;
}

/** The open turns, shown in the thread while they are still being spoken. */
export function openTurns(live: LiveState): Turn[] {
  const out: Turn[] = [];
  if (live.user) out.push({ role: "user", text: live.user.text, spoken: true, speaking: true });
  if (live.agent) out.push({ role: "agent", text: live.agent.text, stream: live.agent.text.length, speaking: live.agentSpeaking });
  return out;
}

export interface LiveInfo {
  on: boolean;
  /** Pressed, but the microphone is not capturing yet: nothing the person says is heard. */
  connecting: boolean;
  name: string;
  agentNow: boolean;
  userNow: boolean;
  amp: number;
  status: string;
  muted: boolean;
}

export function liveInfo(live: LiveState | null, name: string, now: number): LiveInfo {
  if (!live) return { on: false, connecting: false, name: "", agentNow: false, userNow: false, amp: 0, status: "", muted: false };
  const connecting = live.phase === "connecting";
  const agentNow = !connecting && live.agentSpeaking;
  const userNow = !connecting && !live.muted && live.userSpeaking;
  const t = clock(now - (live.listeningSince ?? now));
  let status: string;
  if (live.link === "error") status = "Couldn’t connect";
  else if (live.link === "closed") status = "Not connected";
  else if (connecting || live.link === "connecting") status = "Connecting to GNSIS…";
  else if (userNow) status = `Listening · ${t}`;
  else if (agentNow) status = `${name} is speaking · ${t}`;
  else status = (live.muted ? "You’re muted · " : "Listening · ") + t;
  const amp = agentNow ? live.agentLevel : userNow ? live.userLevel : 0;
  return { on: true, connecting, name, agentNow, userNow, amp, status, muted: live.muted };
}

/** Five bars for the armed button: real level, with a little motion so speech reads as speech. */
export function barHeights(level: number, active: boolean, t: number): number[] {
  return [0, 1, 2, 3, 4].map((i) => {
    if (!active) return i === 2 ? 8 : 5;
    const wobble = 0.25 * Math.abs(Math.sin(t * 0.9 + i * 1.3));
    const amp = Math.min(1, level * 1.4 + wobble);
    return Math.round(6 + amp * 16);
  });
}

const clamp = (n: number) => (Number.isFinite(n) ? Math.max(0, Math.min(1, n)) : 0);
