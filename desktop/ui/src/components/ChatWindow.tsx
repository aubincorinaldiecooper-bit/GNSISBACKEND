import { AGENT_ANSWER, AGENT_STEPS, APPROVAL, STEP_TICKS, type Conv, type Turn } from "../demo/data";
import { useState } from "react";
import { actions, activity, getState, isWorking, liveTurnsFor, phrase, presence, setState, useStore, type State } from "../store/store";
import { liveThinking } from "../store/live";
import { AgentFace, Face, ThinkingDots } from "../lib/face";
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
          <Pending s={s} c={conv} />
          {conv.stopped && <div className="muted small">Stopped</div>}
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
      <ActivityButton s={s} />
      {s.active !== "gnsis" && (
        <button type="button" className="icon-btn" aria-label="Open your chat with GNSIS" onClick={() => actions.openAgent("gnsis")}><I.Plus /></button>
      )}
    </div>
  );
}

/**
 * Opens and closes the Activity drawer. It never opens by itself: an agent
 * that needs the person turns it orange, one at work gives it a moving dot,
 * and finished results the person has not seen are counted on it.
 */
function ActivityButton({ s }: { s: State }) {
  const open = !s.panelHidden;
  const c = s.convs[s.active];
  // In an agent's chat the drawer shows that agent's work, so the button says
  // so and carries no marks for the others; their faces in the tabs do that.
  const home = !c || c.panel === "agents";
  const a = home ? activity(s) : { needs: 0, working: 0, unread: 0 };
  const note = a.needs ? `, ${a.needs} need${a.needs === 1 ? "s" : ""} you` : a.working ? `, ${a.working} working` : a.unread ? `, ${a.unread} new` : "";
  const what = home ? "activity" : `${c.title}’s work`;
  return (
    <button
      type="button"
      className={"icon-btn activity-btn" + (open ? " is-open" : "") + (a.needs ? " needs" : "")}
      aria-label={(open ? `Hide ${what}` : `Show ${what}`) + note}
      aria-pressed={open}
      onClick={actions.toggleActivity}
    >
      <I.PanelIcon size={20} />
      {a.needs > 0 ? (
        <span className="mark-needs" aria-hidden="true">!</span>
      ) : a.working > 0 ? (
        <span className="mark-dot is-working activity-working" aria-hidden="true" />
      ) : a.unread > 0 ? (
        <span className="mark-badge" aria-hidden="true">{a.unread}</span>
      ) : null}
    </button>
  );
}

/**
 * The wait after a typed message, until GNSIS answers: "Sending…" until the
 * runtime has it, then "Thinking". A step GNSIS is taking shows in the steps
 * instead, and a reply arriving speaks for itself.
 */
function Pending({ s, c }: { s: State; c: Conv }) {
  if (s.awaiting?.to !== c.id || s.reply?.to === c.id || s.working?.to === c.id) return null;
  if (s.awaiting.state === "sent") {
    return (
      <div className="pending-line" role="status" aria-live="polite">
        <ThinkingDots /> <span>Sending…</span>
      </div>
    );
  }
  return <Thinking />;
}

const Thinking = () => (
  <div className="thinking-row" role="status" aria-live="polite">
    <span className="shimmer">Thinking</span>
  </div>
);

const DONE = "Done: ";

/** One step GNSIS took: a mark for how it ended, and the host's own sentence. */
function StepRow({ turn }: { turn: Turn }) {
  const state = turn.step!.state;
  // The check already says it is done.
  const text = state === "done" && turn.text.startsWith(DONE) ? turn.text.slice(DONE.length) : turn.text;
  const mark =
    state === "done" ? <I.Check size={14} sw={2.4} /> :
    state === "waiting" ? <I.Hand size={14} /> :
    state === "needs_permission" ? <I.Lock size={13} /> :
    <I.Close size={14} sw={2.2} />;
  return (
    <div className={`step-row is-${state}`}>
      <span className="step-mark" aria-hidden="true">{mark}</span>
      <span>{text}</span>
    </div>
  );
}

function workedFor(steps: Turn[]): string {
  const first = steps[0]?.step;
  const last = steps[steps.length - 1]?.step;
  if (!first || !last) return "Worked on it";
  const ms = last.endedAt - (first.startedAt ?? first.endedAt);
  return `Worked for ${Math.max(1, Math.round(ms / 1000))}s`;
}

/**
 * The steps GNSIS took on the computer, the way AI chats show their work:
 * each step as it happens, the one running now moving; once GNSIS has
 * answered, one line ("Worked for 6s") that opens to show them. A step that
 * failed, was refused or needs a permission keeps the list open.
 */
function Steps({ steps, running, settled }: { steps: Turn[]; running: string | null; settled: boolean }) {
  const [open, setOpen] = useState(false);
  const trouble = steps.some((t) => t.step && t.step.state !== "done" && t.step.state !== "waiting");
  const folds = settled && !trouble && !running && steps.length > 0;
  if (folds && !open) {
    return (
      <button type="button" className="steps-summary" aria-expanded={false} onClick={() => setOpen(true)}>
        {workedFor(steps)} <I.ChevronRight size={14} />
      </button>
    );
  }
  return (
    <div className="steps-block">
      {folds && (
        <button type="button" className="steps-summary" aria-expanded onClick={() => setOpen(false)}>
          {workedFor(steps)} <I.ChevronDown size={14} />
        </button>
      )}
      {steps.map((t, i) => <StepRow key={i} turn={t} />)}
      {running && (
        <div className="step-row is-running" role="status" aria-live="polite">
          <span className="step-mark" aria-hidden="true"><span className="step-spin" /></span>
          <span className="shimmer">{running}</span>
        </div>
      )}
    </div>
  );
}

/**
 * What the person said, typed or spoken, shown as their words. A spoken turn
 * whose words are not available (the host has no speech-to-text) shows
 * nothing: never invented words, and no voice marks in the chat.
 */
function UserBubble({ turn }: { turn: Turn }) {
  if (turn.spoken && !turn.text) return null;
  const pending = !!turn.speaking;
  return (
    <div className="user-msg" aria-live={pending ? "polite" : undefined}>
      <div className={"bubble" + (pending ? " pending" : "")}>{turn.text}</div>
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

type Piece = { kind: "turn"; turn: Turn; i: number } | { kind: "steps"; steps: Turn[]; i: number };

/** Consecutive steps are one group. */
function pieces(turns: Turn[]): Piece[] {
  const out: Piece[] = [];
  turns.forEach((turn, i) => {
    const last = out[out.length - 1];
    if (turn.step && last?.kind === "steps") last.steps.push(turn);
    else if (turn.step) out.push({ kind: "steps", steps: [turn], i });
    else out.push({ kind: "turn", turn, i });
  });
  return out;
}

function Turns({ s, c }: { s: State; c: Conv }) {
  const list = pieces(c.turns);
  const running = s.working?.to === c.id ? s.working.text : null;
  // The step running now sits where it began: after what is already said,
  // above words still arriving. It joins the steps just before it, or starts a group.
  if (running && list[list.length - 1]?.kind !== "steps") list.push({ kind: "steps", steps: [], i: c.turns.length });
  const runningAt = running ? list.length - 1 : -1;
  liveTurnsFor(s, c.id).forEach((turn, j) => list.push({ kind: "turn", turn, i: c.turns.length + j }));
  // A typed message still waiting for its answer shows its own wait below (Pending), so "Thinking" appears once.
  const thinking = s.live?.to === c.id && liveThinking(s.live, s.now) && s.awaiting?.to !== c.id;
  return (
    <>
      {list.map((p, k) => {
        if (p.kind === "steps") {
          // Settled once GNSIS has said something after its steps.
          const answered = list.slice(k + 1).some((q) => q.kind === "turn" && q.turn.role === "agent" && !!q.turn.text.trim());
          return <Steps key={`steps-${p.i}`} steps={p.steps} running={k === runningAt ? running : null} settled={answered} />;
        }
        const tu = p.turn;
        if (tu.role === "system") return <div key={p.i} className="system-line">{tu.text}</div>;
        if (tu.role === "user") return <UserBubble key={p.i} turn={tu} />;
        const shown = tu.text.slice(0, tu.stream ?? tu.text.length);
        const streaming = ((tu.stream ?? 0) < tu.text.length && !c.stopped) || !!tu.speaking;
        return <div key={p.i} className="answer">{shown}{streaming && <Caret />}</div>;
      })}
      {thinking && <Thinking />}
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
      <div className="bubble pending">{firm}<span className="muted">{partial}</span></div>
    </div>
  );
}
