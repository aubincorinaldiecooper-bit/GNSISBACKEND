import { useEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { createPortal } from "react-dom";
import type { VisionSource } from "../host";
import { actions, getHost, useStore } from "../store/store";
import * as I from "./Icons";

/**
 * What is being shared with GNSIS, in the Activity panel: the same picture
 * the host is sharing, live. Resting, it is a small card; "Open" brings it up
 * large, and Collapse, Esc or a click outside puts it back. It never shows a
 * picture the host is not actually sharing: with nothing shared there is
 * nothing to see, and a host that cannot show its picture says so instead.
 * GNSIS itself gets smaller still pictures of it, not this live view, and
 * the card says so rather than claim to be exactly what GNSIS sees.
 */
export function ScreenCard() {
  const caps = useStore((s) => s.caps);
  const vision = useStore((s) => s.vision);
  // Frames only reach GNSIS while the connection is up.
  const reaching = useStore((s) => s.link === "ready");
  const [open, setOpen] = useState(false);
  const opener = useRef<HTMLButtonElement>(null);
  const foot = useRef<HTMLDivElement>(null);
  // Where keyboard focus goes once the page has settled: back to the picture
  // after the large view collapses, or to the card's main button after one of
  // its buttons was used or sharing stopped under the large view.
  const returnTo = useRef<"picture" | "button" | null>(null);
  const on = vision.state === "on";
  const stream = on ? getHost()?.visionStream?.() ?? null : null;
  const what = vision.source === "camera" ? "camera" : "screen";

  // Sharing stopped (by the person, the system, or a failure): the large view
  // goes with it, and focus lands on the card's own buttons, not the page.
  useEffect(() => {
    if (on || !open) return;
    setOpen(false);
    returnTo.current = "button";
  }, [on, open]);

  // Never while the large view is still open: the rest of the page is out of reach then.
  useEffect(() => {
    if (!returnTo.current || open) return;
    const target = returnTo.current === "picture" ? opener.current : foot.current?.querySelector<HTMLButtonElement>("[data-primary]");
    returnTo.current = null;
    target?.focus();
  }, [vision.state, vision.source, open]);

  const use = (next: VisionSource | null) => {
    returnTo.current = "button";
    actions.setVision(next);
  };

  let frame;
  if (on && stream) {
    frame = (
      <button ref={opener} type="button" className="screen-frame is-live" aria-label={`Open your ${what}, as shared with GNSIS`} onClick={() => setOpen(true)}>
        <Picture stream={stream} />
        <span className="screen-open" aria-hidden="true"><I.Expand size={16} /> Open</span>
      </button>
    );
  } else if (on) {
    frame = <div className="screen-frame is-empty"><I.Screen size={26} /><span>Your {what} is being shared with GNSIS. This app can’t show you the picture here.</span></div>;
  } else if (vision.state === "starting") {
    frame = <div className="screen-frame is-dark" role="status"><span className="screen-spin" aria-hidden="true" /><span>Connecting to your {what}…</span></div>;
  } else {
    const trouble = (vision.state === "denied" || vision.state === "error") && vision.detail;
    frame = <div className="screen-frame is-empty" role={trouble ? "alert" : undefined}><I.EyeOff size={26} /><span>{trouble || "You aren’t sharing your screen or camera with GNSIS."}</span></div>;
  }

  const failed = vision.state === "denied" || vision.state === "error";
  const footer = () => {
    if (on) {
      return [
        <span key="dot" className={"live-dot" + (reaching ? "" : " is-paused")} aria-hidden="true" />,
        <span key="text" className="grow small muted">{reaching ? `Your ${what}` : `Paused: GNSIS isn’t connected, so it isn’t getting your ${what}`}</span>,
        <button key="primary" data-primary type="button" className="btn-secondary compact" onClick={() => use(null)}>Stop sharing</button>,
      ];
    }
    if (vision.state === "starting") {
      return [
        <span key="text" className="grow" />,
        <button key="primary" data-primary type="button" className="btn-secondary compact" onClick={() => use(null)}>Cancel</button>,
      ];
    }
    const cameraAgain = failed && vision.source === "camera";
    return [
      <span key="text" className="grow" />,
      caps.camera && (
        <button key="secondary" type="button" className="btn-secondary compact" onClick={() => use("camera")}>
          <I.Camera size={16} /> {cameraAgain ? "Try the camera again" : "Use camera"}
        </button>
      ),
      caps.screen && (
        <button key="primary" data-primary type="button" className="btn-dark compact" onClick={() => use("screen")}>
          <I.Screen size={16} /> {failed && vision.source === "screen" ? "Try sharing again" : "Share my screen"}
        </button>
      ),
    ];
  };

  return (
    <div className="card screen-card">
      <div className="card-title">Shared with GNSIS</div>
      {frame}
      {/* Keyed children: the main button stays the same element from Share to Cancel to Stop sharing, so keyboard focus stays on it. */}
      <div ref={foot} className="screen-foot">{footer()}</div>
      {open && stream && (
        <Viewer
          stream={stream}
          what={what}
          reaching={reaching}
          onCollapse={() => {
            returnTo.current = "picture";
            setOpen(false);
          }}
          onStop={() => use(null)}
        />
      )}
    </div>
  );
}

/**
 * The large view: over everything, until Collapse, Esc, or a click outside
 * it. While it is open, the rest of the page is out of reach (inert), and Tab
 * moves only between its own buttons.
 */
function Viewer({ stream, what, reaching, onCollapse, onStop }: { stream: MediaStream; what: string; reaching: boolean; onCollapse: () => void; onStop: () => void }) {
  const dialog = useRef<HTMLDivElement>(null);
  const close = useRef<HTMLButtonElement>(null);
  // The latest handler, read when a key is pressed: the page re-renders many
  // times a second during live voice, and none of that may move focus.
  const collapse = useRef(onCollapse);
  collapse.current = onCollapse;

  useEffect(() => {
    close.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      collapse.current();
    };
    window.addEventListener("keydown", onKey, true);
    // Everything outside the view is out of reach while it is open.
    const scrim = dialog.current?.parentElement;
    const others = Array.from(document.body.children).filter((el) => el !== scrim && !(el as HTMLElement).inert) as HTMLElement[];
    for (const el of others) el.inert = true;
    return () => {
      window.removeEventListener("keydown", onKey, true);
      for (const el of others) el.inert = false;
    };
  }, []);

  // Tab and Shift+Tab stay among the view's own buttons.
  const trap = (e: ReactKeyboardEvent) => {
    if (e.key !== "Tab") return;
    const buttons = Array.from(dialog.current?.querySelectorAll("button") ?? []);
    if (buttons.length === 0) return;
    const at = buttons.indexOf(document.activeElement as HTMLButtonElement);
    const next = (at + (e.shiftKey ? -1 : 1) + buttons.length) % buttons.length;
    e.preventDefault();
    buttons[next].focus();
  };

  return createPortal(
    <div data-hit className="screen-scrim" onClick={() => collapse.current()}>
      <div ref={dialog} role="dialog" aria-modal="true" aria-label={`Your ${what}, as shared with GNSIS`} className="screen-viewer" onClick={(e) => e.stopPropagation()} onKeyDown={trap}>
        <header className="screen-bar">
          <span className={"live-dot" + (reaching ? "" : " is-paused")} aria-hidden="true" />
          <span className="grow"><strong>Your {what}</strong><span className="small muted">{reaching ? "Shared with GNSIS" : "Paused: GNSIS isn’t connected"}</span></span>
          <button type="button" className="btn-secondary compact" onClick={onStop}>Stop sharing</button>
          <button ref={close} type="button" className="icon-btn" aria-label="Collapse" title="Collapse (Esc)" onClick={() => collapse.current()}><I.Collapse /></button>
        </header>
        <Picture stream={stream} />
      </div>
    </div>,
    document.body,
  );
}

function Picture({ stream }: { stream: MediaStream }) {
  const ref = useRef<HTMLVideoElement>(null);
  useEffect(() => {
    const v = ref.current;
    if (!v) return;
    v.srcObject = stream;
    void v.play().catch(() => {});
    return () => {
      v.srcObject = null;
    };
  }, [stream]);
  return <video ref={ref} className="screen-video" muted playsInline autoPlay aria-hidden="true" />;
}
