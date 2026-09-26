import { AGENT_ANSWER, AGENT_STEPS, APPROVAL, STEP_TICKS, type Conv, type Turn } from "../demo/data";
import { actions, getState, isWorking, liveTurnsFor, phrase, presence, setState, useStore, type State } from "../store/store";
import { AgentFace, Face } from "../lib/face";
import { clock } from "../lib/platform";
import * as I from "./Icons";

export function ChatWindow({ height }: { height: number | "auto" }) {
  const s = useStore((x) => x);
  const conv = s.convs[s.active];
  if (!conv) return null;
  return (
    <section data-hit className="window glass" style={{ height }} aria-label={`Chat with ${conv.title}`}>
      <Tabs s={s} />
      <div className="thread-scroll">
        <div className="thread">
          {conv.req && <UserBubble turn={{ role: "user", text: conv.req, spoken: conv.spoken }} />}
          {conv.kind === "chat" && <ChatBody s={s} c={conv} />}
          {conv.kind === "agent" && <AgentSteps s={s} c={conv} />}
          <Turns s={s} c={conv} />
          {s.listening && s.listenTo === conv.id && <LiveBubble s={s} />}
          {conv.stopped && <div className="muted small">Stopped</div>}
          {s.panelHidden && (
            <button type="button" className="ghost-btn start" onClick={() => setState({ panelHidden: false })}>
              <I.PanelIcon size={18} /> Show panel
            </button>
          )}
        </div>
      </div>
    </section>
  );
}

function Tabs({ s }: { s: State }) {
  return (
    <div className="tabs">
      <div className="tabs-row">
        {s.tabs.map((id) => {
          const c = s.convs[id];
          if (!c) return null;
          const on = id === s.active;
          const home = id === "gnsis";
          const p = home ? undefined : presence(c, s);
          const face = home ? <Face name={s.identity?.publicId ?? "GNSIS"} gnsis size={22} /> : <AgentFace name={c.title} size={22} p={p} markSize="sm" pop={!!c.bornT && s.t - c.bornT < 8} />;
          return on ? (
            <div key={id} className="tab on">
              <span className="tab-label">{face}<span className="ellipsis">{c.title}</span></span>
              <button type="button" className="tab-close" aria-label={`Close ${c.title}`} onClick={() => actions.closeTab(id)}><I.Close size={15} /></button>
            </div>
          ) : (
            <button key={id} type="button" className="tab off" aria-label={`${c.title}${p ? ", " + p.status.toLowerCase() : ""}`} onClick={() => actions.openAgent(id)}>
              {face}
            </button>
          );
        })}
      </div>
      <button type="button" className="icon-btn" aria-label="New chat" onClick={() => actions.openAgent("gnsis")}><I.Plus /></button>
    </div>
  );
}

/**
 * What the person said. Typed text is shown as typed. A spoken turn carries a
 * "Spoken" tag; when its words are not available (the host has no
 * speech-to-text) it shows how long they spoke, never invented words.
 */
function UserBubble({ turn }: { turn: Turn }) {
  const pending = !!turn.speaking;
  const wordless = turn.spoken && !turn.text;
  return (
    <div className="user-msg" aria-live={pending ? "polite" : undefined}>
      {turn.spoken && (
        <span className="spoken"><I.Mic size={14} sw={2} /> {pending ? "Listening…" : "Spoken"}</span>
      )}
      {wordless ? (
        <div className={"bubble spoken-only" + (pending ? " pending" : "")} aria-label={pending ? "You are speaking" : `You spoke for ${clock(turn.spokenMs ?? 0)}`}>
          <span className="spoken-bars" aria-hidden="true"><span /><span /><span /><span /><span /></span>
          {!pending && <span className="muted small">{clock(turn.spokenMs ?? 0)}</span>}
        </div>
      ) : (
        <div className={"bubble" + (pending ? " pending" : "")}>{turn.text}</div>
      )}
    </div>
  );
}

const Caret = () => <span className="caret" aria-hidden="true" />;

function ChatBody({ s, c }: { s: State; c: Conv }) {
  const running = !c.stopped && c.stream < c.ans.length;
  const pending = c.id === "roof" && c.approval === "pending" && !isWorking(c);
  const follow = c.follow ? c.follow.slice(0, c.followStream ?? 0) : "";
  return (
    <>
      <div className="answer">{c.ans.slice(0, c.stream)}{running && <Caret />}</div>
      {pending && <Approval s={s} />}
      {c.approval === "answered" && (
        <div className="answered"><span className="answered-check"><I.Check size={12} sw={3} /></span>{c.answeredLabel}</div>
      )}
      {follow && <div className="answer">{follow}{(c.followStream ?? 0) < (c.follow?.length ?? 0) && !c.stopped && <Caret />}</div>}
    </>
  );
}

function Approval({ s }: { s: State }) {
  const ready = s.apPick !== null || !!s.apCustom.trim();
  return (
    <div className="approval">
      <div className="approval-q">{APPROVAL.question}</div>
      <div className="approval-options">
        {APPROVAL.options.map((label, i) => {
          const on = s.apPick === i;
          return (
            <button key={label} type="button" aria-pressed={on} className={"radio-row" + (on ? " on" : "")} onClick={() => {
              setState({ apPick: i });
              window.setTimeout(() => { if (stillPicked(i)) actions.answerApproval(i); }, 480);
            }}>
              <span className="radio"><span /></span>{label}
            </button>
          );
        })}
        <input
          className="approval-custom" type="text" aria-label="Something else" placeholder="Something else…"
          value={s.apCustom}
          onChange={(e) => setState({ apCustom: e.target.value, apPick: null })}
          onKeyDown={(e) => { if (e.key === "Enter" && s.apCustom.trim()) { e.preventDefault(); actions.answerApproval("custom"); } }}
        />
      </div>
      <div className="approval-foot">
        <button type="button" className="pill quiet" onClick={() => actions.answerApproval("skip")}>Skip</button>
        <button type="button" className="pill accent" disabled={!ready} onClick={() => actions.answerApproval(s.apPick ?? "custom")}>Send</button>
      </div>
    </div>
  );
}
// single-choice answers advance on their own, unless the choice changed in the meantime
function stillPicked(i: number) {
  return getState().apPick === i;
}

function AgentSteps({ s, c }: { s: State; c: Conv }) {
  const n = AGENT_STEPS.length;
  const done = Math.min(n, Math.floor((c.agentT ?? 0) / STEP_TICKS));
  const open = c.stepsOpen !== false;
  const stream = c.agentStream ?? 0;
  const pulse = 3 + 4 * Math.abs(Math.sin(s.t * 0.35));
  return (
    <>
      <div className="answer muted">Opening a browser. You can watch me work in my panel on the right, or click in to take over.</div>
      <button type="button" className="steps-toggle" aria-expanded={open} onClick={actions.toggleSteps}>
        <I.Globe /> {done >= n ? "Found the recipe" : "Finding the recipe"} {open ? <I.ChevronUp size={16} /> : <I.ChevronDown size={16} />}
      </button>
      {open && (
        <div className="steps">
          {AGENT_STEPS.slice(0, Math.min(n, done + 1)).map((st, i) => (
            <div key={st.label} className="step">
              {i < done ? <I.Check size={18} sw={2} /> : c.stopped ? <span className="step-idle" /> : <span className="step-live"><span style={{ boxShadow: `0 0 0 ${pulse}px rgba(46, 84, 230, 0.25)` }} /></span>}
              <span>{st.label}</span>
              {i < done && st.detail && <span className="step-detail">{st.detail}</span>}
            </div>
          ))}
        </div>
      )}
      {done >= n && stream > 0 && <div className="answer">{AGENT_ANSWER.slice(0, stream)}{isWorking(c) && <Caret />}</div>}
    </>
  );
}

function Turns({ s, c }: { s: State; c: Conv }) {
  const turns: Turn[] = [...c.turns, ...liveTurnsFor(s, c.id)];
  return (
    <>
      {turns.map((tu, i) => {
        if (tu.role === "system") return <div key={i} className="system-line"><I.Wave size={16} sw={2} /> {tu.text}</div>;
        if (tu.role === "user") return <UserBubble key={i} turn={tu} />;
        const shown = tu.text.slice(0, tu.stream ?? tu.text.length);
        const streaming = ((tu.stream ?? 0) < tu.text.length && !c.stopped) || !!tu.speaking;
        return <div key={i} className="answer">{shown}{streaming && <Caret />}</div>;
      })}
    </>
  );
}

/** Dictation in progress: the words so far, the last one still being recognized. */
function LiveBubble({ s }: { s: State }) {
  const words = phrase(s).split(" ");
  const firm = s.heard > 1 ? words.slice(0, s.heard - 1).join(" ") + " " : "";
  const partial = s.heard > 0 ? words[s.heard - 1] : "";
  return (
    <div className="user-msg" aria-live="polite">
      <span className="spoken"><I.Mic size={14} sw={2} /> Listening…</span>
      <div className="bubble pending">{firm}<span className="muted">{partial}</span></div>
    </div>
  );
}
