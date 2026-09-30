import { useRef } from "react";
import { AgentFace, Face, ThinkingDots } from "../lib/face";
import { actions, isWorking, liveInfoFor, phrase, presence, setState, useStore, type State } from "../store/store";
import { barHeights } from "../store/live";
import * as I from "./Icons";

export interface Geometry {
  shellLeft: number;
  shellW: number;
  shellH: number;
}

/** Dock items, most urgent first; finished agents you've read leave the dock. */
export function rankedAgents(s: State) {
  return s.agentIds
    .map((id, i) => ({ id, i, c: s.convs[id], p: presence(s.convs[id], s) }))
    .sort((a, b) => a.p.rank - b.p.rank || a.i - b.i);
}

export function dockGeometry(s: State) {
  const live = rankedAgents(s).filter((r) => !r.c.archived);
  const k = Math.min(4, live.length);
  const more = live.length - k;
  const has = k > 0;
  const count = (has ? 5 : 4) + k + (more > 0 ? 1 : 0);
  const width = 28 + 64 + 21 + 64 * k + (more > 0 ? 52 : 0) + (has ? 21 : 0) + 48 + 52 + 4 * (count - 1) + 2;
  return { shown: live.slice(0, 4), more, width };
}

export function Shell({ g }: { g: Geometry }) {
  const s = useStore((x) => x);
  const bar = s.mode === "bar";
  return (
    <div
      data-hit
      className="shell glass"
      style={{ left: g.shellLeft, width: g.shellW, height: g.shellH, borderRadius: g.shellH / 2 }}
    >
      <DockLayer s={s} hidden={bar} />
      <BarLayer s={s} hidden={!bar} width={bar ? g.shellW - 2 : 718} />
    </div>
  );
}

function DockLayer({ s, hidden }: { s: State; hidden: boolean }) {
  const { shown, more } = dockGeometry(s);
  const news = !!s.convs.gnsis?.unread;
  return (
    <div className="dock-layer" aria-hidden={hidden} style={{ opacity: hidden ? 0 : 1, pointerEvents: hidden ? "none" : "auto" }}>
      <button type="button" className="dock-sun" aria-label={news ? "Open your chat with GNSIS, new result" : "Open your chat with GNSIS"} onClick={() => actions.openAgent("gnsis")}>
        <Face name={s.identity?.publicId ?? "GNSIS"} size={50} gnsis />
        {/* Something landed in GNSIS's chat while it was closed. */}
        {news && <span className="mark-badge dock-news" aria-hidden="true">1</span>}
      </button>
      <span className="divider" aria-hidden="true" />
      {shown.map((r) => (
        <button key={r.id} type="button" className="dock-agent" aria-label={`${r.c.title}, ${r.p.status.toLowerCase()}`} onClick={() => actions.openAgent(r.id)}>
          <AgentFace name={r.c.title} size={48} p={r.p} markSize="lg" pop={r.c.bornT !== undefined && s.t - r.c.bornT < 8} />
          {r.p.working && <span className="dock-dots"><ThinkingDots /></span>}
        </button>
      ))}
      {more > 0 && (
        <button type="button" className="dock-more" aria-label={`Show all agents, ${more} more`} onClick={() => setState({ dockMenu: !s.dockMenu })}>
          +{more}
        </button>
      )}
      {shown.length > 0 && <span className="divider" aria-hidden="true" />}
      <button type="button" className="icon-btn round" aria-label="Menu" aria-expanded={s.dockMenu} onClick={() => setState({ dockMenu: !s.dockMenu })}>
        <I.Menu size={22} />
      </button>
      <button
        type="button"
        className="voice-btn"
        aria-label={s.caps.voice ? "Talk live with GNSIS" : "Live voice is not available on this host"}
        disabled={!s.caps.voice}
        onClick={() => actions.startLive("gnsis")}
      >
        <I.Wave size={22} />
      </button>
    </div>
  );
}

function BarLayer({ s, hidden, width }: { s: State; hidden: boolean; width: number }) {
  const input = useRef<HTMLInputElement>(null);
  const conv = s.winOpen ? s.convs[s.active] : undefined;
  const addressee = conv ?? s.convs.gnsis;
  const isHome = !conv || conv.id === "gnsis";
  const working = !!conv && isWorking(conv);
  const hasText = s.text.length > 0;
  const live = liveInfoFor(s);
  const liveHere = live.on && s.live?.to === s.active;
  const words = phrase(s).split(" ");
  // While live the field stays empty: the voice button shows what is happening.
  const placeholder = live.on
    ? ""
    : !s.caps.text && !s.demo
      ? "Press the voice button to talk, / for commands"
      : conv && !isHome ? `Message ${conv.title}…` : "Talk or type, / for commands";
  const canSee = s.caps.screen || s.caps.camera;
  const seeing = s.vision.state === "on" || s.vision.state === "starting";
  const trouble = (s.vision.state === "denied" || s.vision.state === "error") && !!s.vision.detail;
  // The face collapses the chat to the dock, which also ends live voice.
  const closeLabel = s.live
    ? `Close the chat and end live voice with ${s.convs[s.live.to]?.title ?? "GNSIS"}`
    : s.agentIds.length ? "Close the chat and show all agents" : "Close the chat";

  const faceStyle = liveHere
    ? { transform: `scale(${1 + (live.agentNow ? 0.08 * live.amp : 0)}) translateY(${live.userNow ? 1.5 : 0}px)` }
    : undefined;

  const bars = Array.from({ length: 64 }, (_, i) => {
    const k = s.t - (63 - i);
    const speaking = k >= 2 && k < words.length * 4 + 2;
    const amp = speaking ? 0.2 + 0.8 * Math.abs(Math.sin(k * 1.9) * Math.cos(k * 0.47 + 1)) : 0.05 + 0.05 * Math.abs(Math.sin(k * 3.1));
    return { h: Math.round(4 + amp * 28), o: 0.3 + (0.7 * i) / 63 };
  });

  return (
    <div className="bar-layer" aria-hidden={hidden} style={{ width, opacity: hidden ? 0 : 1, pointerEvents: hidden ? "none" : "auto" }}>
      <button type="button" className="addr-face" aria-label={closeLabel} title={closeLabel} onClick={actions.toDock}>
        <Face name={isHome ? s.identity?.publicId ?? "GNSIS" : addressee.title} gnsis={isHome} size={34} style={faceStyle} pop={!!addressee.bornT && s.t - addressee.bornT < 8} />
      </button>
      {canSee ? (
        <button
          type="button"
          className="icon-btn round"
          aria-label={
            s.vision.state === "starting"
              ? `Starting to share your ${s.vision.source}. Change what GNSIS sees`
              : seeing && s.link !== "ready"
                ? `Your ${s.vision.source} is shared, but GNSIS isn’t connected, so it isn’t getting it. Change what GNSIS sees`
              : seeing
              ? `GNSIS is looking at your ${s.vision.source}. Change what it sees`
              : trouble
                ? `Let GNSIS see your screen or camera. It didn’t start: ${s.vision.detail}`
                : "Let GNSIS see your screen or camera"
          }
          aria-expanded={s.visionMenu}
          aria-pressed={seeing}
          onClick={() => setState({ visionMenu: !s.visionMenu })}
        >
          <I.Plus size={22} />
        </button>
      ) : (
        <button type="button" className="icon-btn round" aria-label="Attach" disabled>
          <I.Plus size={22} />
        </button>
      )}

      {!s.listening ? (
        <>
          <div className="bar-input">
            <input
              ref={input}
              type="text"
              aria-label={`Message ${addressee.title}`}
              value={s.text}
              onChange={(e) => setState({ text: e.target.value })}
              onKeyDown={(e) => {
                if (e.key === "Enter") { e.preventDefault(); actions.send(); }
                else if (e.key === "Escape") { if (s.text) setState({ text: "" }); else actions.toDock(); }
              }}
            />
            {!hasText && placeholder && <span className="bar-placeholder" aria-hidden="true">{placeholder}</span>}
          </div>
          <button type="button" className="model-btn" disabled title="Choosing a model isn’t in this build yet">Auto <I.ChevronDown size={16} /></button>
          {live.on && !live.connecting ? (
            <button type="button" className={"icon-btn round mute" + (live.muted ? " is-muted" : "")} aria-label={live.muted ? "Unmute" : "Mute"} aria-pressed={live.muted} onClick={actions.toggleMute}>
              {live.muted ? <I.MicOff size={21} /> : <I.Mic size={21} />}
            </button>
          ) : s.caps.transcript ? (
            <button type="button" className="icon-btn round" aria-label="Dictate a message" onClick={actions.mic}>
              <I.Mic size={21} />
            </button>
          ) : null}
          {/* While live, Cancel or End stays put whatever is typed; Enter still sends. */}
          {hasText && !live.on ? (
            <button type="button" className="primary-btn" aria-label="Send" onClick={actions.send}><I.ArrowUp size={22} /></button>
          ) : live.on && live.connecting ? (
            <button type="button" className="primary-btn connecting" aria-label={`Cancel. ${live.status}`} onClick={actions.endLive}>
              <span className="spin" aria-hidden="true" />
            </button>
          ) : live.on ? (
            <button type="button" className="primary-btn armed" aria-label={`End live voice with ${live.name}. ${live.status}`} aria-pressed="true" onClick={actions.endLive}>
              {barHeights(live.amp, live.agentNow || live.userNow, s.t).map((h, i) => <span key={i} style={{ height: h }} />)}
            </button>
          ) : working ? (
            <button type="button" className="primary-btn" aria-label="Stop" onClick={actions.stop}><span className="stop-square" /></button>
          ) : (
            <button
              type="button"
              className="primary-btn"
              aria-label={s.caps.voice ? `Talk live with ${addressee.title}` : "Live voice is not available on this host"}
              disabled={!s.caps.voice}
              onClick={() => actions.startLive(isHome ? "gnsis" : addressee.id)}
            >
              <I.Wave size={22} />
            </button>
          )}
        </>
      ) : (
        <>
          <div className="waveform" role="img" aria-label="Recording your voice">
            {bars.map((b, i) => <span key={i} style={{ height: b.h, opacity: b.o }} />)}
          </div>
          <button type="button" className="icon-btn round" aria-label="Type instead" onClick={() => { actions.typeInstead(); setTimeout(() => input.current?.focus(), 50); }}>
            <I.Keyboard size={22} />
          </button>
          <button type="button" className="primary-btn listening" aria-label="Stop dictating" onClick={actions.finishListening}>
            <span className="stop-square" />
          </button>
        </>
      )}
    </div>
  );
}
