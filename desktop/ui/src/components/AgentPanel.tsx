import { useState } from "react";
import { AGENT_TOTAL, ROOF_DRAFT, STEP_TICKS, type Conv } from "../demo/data";
import { actions, isWorking, presence, setState, useStore, type State } from "../store/store";
import { AgentFace, Face, ThinkingDots } from "../lib/face";
import { copyText } from "../lib/platform";
import { rankedAgents } from "./Shell";
import * as I from "./Icons";

export function AgentPanel({ left, width, height }: { left: number; width: number; height: number }) {
  const s = useStore((x) => x);
  const c = s.convs[s.active];
  if (!c) return null;
  const home = c.id === "gnsis";
  const p = presence(c, s);
  const liveHere = !!s.live && s.live.to === c.id;
  const status = liveHere ? "Live" : home ? "Your assistant" : p.status;
  return (
    <aside data-hit className="panel glass" style={{ left, width, height }} aria-label={`${c.title} panel`}>
      <header className="panel-head">
        <div className="panel-who">
          {home ? <Face name={s.identity?.publicId ?? "GNSIS"} gnsis size={36} /> : <AgentFace name={c.title} size={36} p={p} />}
          <span className="panel-names"><strong>{c.title}</strong><span className="muted small">{status}</span></span>
        </div>
        {c.panel === "browser" && (
          <>
            <button type="button" className="icon-btn" aria-label="Previous screen"><I.ChevronLeft /></button>
            <button type="button" className="icon-btn" aria-label="Next screen"><I.ChevronRight /></button>
            <button type="button" className="icon-btn" aria-label="More options" aria-expanded={s.menuOpen} onClick={() => setState({ menuOpen: !s.menuOpen })}><I.Dots /></button>
          </>
        )}
        <button type="button" className="icon-btn" aria-label="Hide panel" onClick={() => setState({ panelHidden: true, menuOpen: false })}><I.Close /></button>
      </header>
      <div className="panel-body">
        {c.panel === "browser" && <BrowserCard c={c} />}
        {c.panel === "list" && c.list && <ListCard c={c} />}
        {c.panel === "email" && <EmailCard c={c} />}
        {c.panel === "watch" && <WatchCard c={c} />}
        {c.panel === "agents" && <AgentsCard s={s} />}
        {s.menuOpen && (
          <div className="popover panel-menu">
            <button type="button" onClick={() => setState({ menuOpen: false })}><I.Hand size={18} /> Take over</button>
            <button type="button" onClick={() => setState({ menuOpen: false })}><I.External size={18} /> Open in browser</button>
          </div>
        )}
      </div>
    </aside>
  );
}

function BrowserCard({ c }: { c: Conv }) {
  const done = Math.min(4, Math.floor((c.agentT ?? 0) / STEP_TICKS));
  const spots = [[60, 10], [150, 30], [140, 62], [70, 130], [360, 180]];
  const [x, y] = spots[Math.min(done, spots.length - 1)];
  const loading = done === 0 && (c.agentT ?? 0) < AGENT_TOTAL;
  return (
    <>
      <div className="browser">
        <div className="browser-chrome">
          <span className="dot" /><span className="dot" /><span className="dot" />
          <div className="browser-url"><I.Lock size={12} /> recipes.example/cacio-e-pepe</div>
          <span style={{ width: 47 }} />
        </div>
        <div className="browser-page">
          {loading && <div className="loadbar" style={{ width: `${Math.round(((c.agentT ?? 0) / STEP_TICKS) * 100)}%` }} />}
          <h1>Cacio e pepe</h1>
          <div className="muted">Serves 2, ready in 20 minutes</div>
          <div className="recipe-cols">
            <div><h2>Ingredients</h2><ul><li>200 g spaghetti</li><li>100 g Pecorino Romano, finely grated</li><li>2 tsp black peppercorns, cracked</li><li>Salt for the pasta water</li></ul></div>
            <div><h2>Method</h2><ol><li>Toast the pepper in a dry pan until fragrant.</li><li>Cook the pasta and save a mug of its water.</li><li>Off the heat, toss pasta, pepper and cheese with splashes of water until glossy.</li></ol></div>
          </div>
          <svg className="agent-cursor" style={{ left: x, top: y }} width="22" height="22" viewBox="0 0 24 24" aria-hidden="true">
            <path d="M4 3l15 7.5-6.5 1.8L9.7 19z" fill="#141821" stroke="#ffffff" strokeWidth="1.5" strokeLinejoin="round" />
          </svg>
        </div>
      </div>
      <div className="caption">Browser agent’s screen</div>
    </>
  );
}

function ListCard({ c }: { c: Conv }) {
  const list = c.list!;
  const visible = list.progressive ? Math.floor((c.stream / Math.max(1, c.ans.length)) * list.rows.length + 0.0001) : list.rows.length;
  const writing = !!list.progressive && isWorking(c) && c.stream < c.ans.length;
  return (
    <div className="card">
      <div className="card-title">{list.title}</div>
      {list.rows.map((label, i) => (
        <div key={label} className={"list-row" + (i < visible ? " shown" : "")}>
          {list.style === "check" && <span className="checkbox" />}
          {list.style === "num" && <span className="num">{i + 1}</span>}
          <span className="grow">{label}</span>
          {list.style === "chev" && <I.ChevronRight size={16} />}
        </div>
      ))}
      {writing && <div className="writing"><ThinkingDots /> Writing</div>}
    </div>
  );
}

function EmailCard({ c }: { c: Conv }) {
  const [copied, setCopied] = useState(false);
  const n = c.draftStream ?? 0;
  const done = n >= ROOF_DRAFT.length;
  return (
    <div className="card">
      <div className="card-title">Email draft</div>
      <div className="email-text">{ROOF_DRAFT.slice(0, n)}{!done && !c.stopped && <span className="caret" />}</div>
      <div className="card-actions" style={{ opacity: done ? 1 : 0.35 }}>
        <button type="button" className="btn-secondary" onClick={async () => { setCopied(await copyText(ROOF_DRAFT)); }}>{copied ? "Copied" : "Copy"}</button>
        <a className="btn-accent" href={"mailto:?subject=" + encodeURIComponent("Questions about the roof estimate") + "&body=" + encodeURIComponent(ROOF_DRAFT.split("\n").slice(2).join("\n"))}>Open in Mail</a>
      </div>
    </div>
  );
}

function WatchCard({ c }: { c: Conv }) {
  return (
    <div className="card">
      <div className="card-title">Watching your inbox</div>
      <div className="list-row shown"><I.Mail /> <span className="grow">The roofer’s reply about the estimate</span><span className="chip">{c.forever ? "Waiting" : "Stopped"}</span></div>
      {c.forever && <button type="button" className="btn-secondary start" onClick={actions.stopWatching}>Stop watching</button>}
    </div>
  );
}

function AgentsCard({ s }: { s: State }) {
  const rows = rankedAgents(s);
  return (
    <div className="card">
      <div className="card-title">Your agents</div>
      {rows.length === 0 && <p className="muted">No agents yet. When a job needs its own helper, I’ll start one and it shows up here.</p>}
      {rows.map((r) => (
        <button key={r.id} type="button" className="agent-row" aria-label={`Open ${r.c.title}, ${r.p.status.toLowerCase()}`} onClick={() => actions.openAgent(r.id)}>
          <AgentFace name={r.c.title} size={32} p={r.p} />
          <span className="grow"><strong>{r.c.title}</strong><span className="muted small">{r.p.status}</span></span>
          <I.ChevronRight size={16} />
        </button>
      ))}
    </div>
  );
}
