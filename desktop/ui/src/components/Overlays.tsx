import { actions, filteredCommands, getState, setState, useStore } from "../store/store";
import { AgentFace, Face } from "../lib/face";
import { rankedAgents } from "./Shell";
import * as I from "./Icons";

export function Commands() {
  const text = useStore((s) => s.text);
  const list = filteredCommands(text);
  return (
    <div data-hit className="popover commands glass" role="listbox" aria-label="Commands">
      {list.map((c, i) => (
        <button key={c.name} type="button" role="option" aria-selected={i === 0} className={i === 0 ? "on" : ""} onClick={() => setState({ text: c.name + " " })}>
          <strong>{c.name}</strong><span className="muted">{c.desc}</span>
        </button>
      ))}
      {list.length === 0 && <div className="muted pad">No commands match</div>}
      <hr />
      <div className="muted small pad">Type to search commands</div>
    </div>
  );
}

export function DockMenu({ left }: { left: number }) {
  const s = useStore((x) => x);
  const rows = rankedAgents(s);
  return (
    <div data-hit className="popover dock-menu" style={{ left }}>
      <div className="menu-label">All agents</div>
      {rows.length === 0 && <p className="muted small pad">No agents yet. Talk to GNSIS and it will start one when a job needs it.</p>}
      {rows.map((r) => (
        <button key={r.id} type="button" className="agent-row" aria-label={`Open ${r.c.title}, ${r.p.status.toLowerCase()}`} onClick={() => actions.openAgent(r.id)}>
          <AgentFace name={r.c.title} size={30} p={r.p} />
          <span className="grow"><strong>{r.c.title}</strong><span className="muted small">{r.p.status}</span></span>
        </button>
      ))}
      <hr />
      <button type="button" className="menu-item" onClick={() => { actions.openAgent("gnsis"); setState({ dockMenu: false }); }}><I.Pencil size={18} /> New chat</button>
      <button type="button" className="menu-item" onClick={() => setState({ dockMenu: false, settingsOpen: true })}><I.Gear size={18} /> Settings</button>
    </div>
  );
}

/**
 * What GNSIS is allowed to see. The visual sense is one persistent stream —
 * the screen or the camera, never both — so this is a switch, not a list of
 * attachments. "On" is shown only once the runtime has accepted a frame.
 */
export function VisionMenu({ left }: { left: number }) {
  const s = useStore((x) => x);
  const v = s.vision;
  const seeing = v.state === "on" || v.state === "starting";
  let status = "";
  if (v.state === "starting") status = v.source === "screen" ? "Starting to share your screen…" : "Turning the camera on…";
  else if (v.state === "on") status = v.source === "screen" ? "GNSIS can see your screen." : "GNSIS can see through the camera.";
  else if (v.state === "denied") status = v.detail || "Permission was not given.";
  else if (v.state === "error") status = v.detail || "It could not start.";
  return (
    <div data-hit className="popover vision-menu" style={{ left }} role="menu" aria-label="What GNSIS can see">
      <div className="menu-label">What GNSIS can see</div>
      {s.caps.screen && (
        <button type="button" role="menuitemradio" aria-checked={seeing && v.source === "screen"} className={"menu-item" + (seeing && v.source === "screen" ? " on" : "")} onClick={() => actions.setVision("screen")}>
          <I.Screen size={18} /> My screen
        </button>
      )}
      {s.caps.camera && (
        <button type="button" role="menuitemradio" aria-checked={seeing && v.source === "camera"} className={"menu-item" + (seeing && v.source === "camera" ? " on" : "")} onClick={() => actions.setVision("camera")}>
          <I.Camera size={18} /> The camera
        </button>
      )}
      <button type="button" role="menuitemradio" aria-checked={!seeing} className={"menu-item" + (!seeing ? " on" : "")} onClick={() => actions.setVision(null)}>
        <I.EyeOff size={18} /> Nothing
      </button>
      {status && <div className="muted small pad menu-status" role="status">{status}</div>}
    </div>
  );
}

export function Greeting({ left }: { left: number }) {
  return (
    <div data-hit role="status" className="greeting" style={{ left }}>
      <span className="greeting-tail" aria-hidden="true" />
      <strong>Hi, I’m your GNSIS.</strong>
      <span className="muted">Tap my face to open our chat, or press the voice button to talk with me live. When a job needs its own helper, I’ll start an agent and it shows up here.</span>
    </div>
  );
}

export function Toast({ left }: { left: number }) {
  const s = useStore((x) => x);
  const toast = s.toast;
  if (!toast) return null;
  const c = s.convs[toast.id];
  if (!c) return null;
  return (
    <div data-hit role="status" className="toast popover" style={{ left }}>
      {c.id === "gnsis" ? <Face name={s.identity?.publicId ?? "GNSIS"} gnsis size={32} /> : <AgentFace name={c.title} size={32} />}
      <span className="grow"><strong>{c.title}</strong><span className="muted ellipsis">{toast.text}</span></span>
      <button type="button" className="btn-dark" onClick={() => { const id = getState().toast?.id; if (id) actions.openAgent(id); setState({ toast: null }); }}>Open</button>
      <button type="button" className="icon-btn" aria-label="Dismiss" onClick={() => setState({ toast: null })}><I.Close size={16} /></button>
    </div>
  );
}
