import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { actions, getHost, useStore } from "../store/store";
import * as I from "./Icons";

/**
 * What GNSIS is being shown, in the Activity panel: the same picture the host
 * is sharing, live. Resting, it is a small card; "Open" brings it up large,
 * and Collapse or Esc puts it back. It never shows a picture the host is not
 * actually sharing: with nothing shared there is nothing to see, and a host
 * that cannot show its picture says so instead.
 */
export function ScreenCard() {
  const caps = useStore((s) => s.caps);
  const vision = useStore((s) => s.vision);
  const [open, setOpen] = useState(false);
  const opener = useRef<HTMLButtonElement>(null);
  const on = vision.state === "on";
  const stream = on ? getHost()?.visionStream?.() ?? null : null;
  const what = vision.source === "camera" ? "camera" : "screen";

  // Sharing stopped (by the person, the system, or a failure): the viewer goes with it.
  useEffect(() => {
    if (!on) setOpen(false);
  }, [on]);

  const collapse = () => {
    setOpen(false);
    opener.current?.focus();
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
    frame = <div className="screen-frame is-empty"><I.Screen size={26} /><span>GNSIS is getting your {what}. This app can’t show you the picture here.</span></div>;
  } else if (vision.state === "starting") {
    frame = <div className="screen-frame is-dark" role="status"><span className="screen-spin" aria-hidden="true" /><span>Connecting to your {what}…</span></div>;
  } else {
    const trouble = (vision.state === "denied" || vision.state === "error") && vision.detail;
    frame = <div className="screen-frame is-empty" role={trouble ? "alert" : undefined}><I.EyeOff size={26} /><span>{trouble || "GNSIS isn’t looking at your screen or camera."}</span></div>;
  }

  return (
    <div className="card screen-card">
      <div className="card-title">What GNSIS sees</div>
      {frame}
      <div className="screen-foot">
        {on ? (
          <>
            <span className="live-dot" aria-hidden="true" />
            <span className="grow small muted">Your {what}, shared with GNSIS</span>
            <button type="button" className="btn-secondary compact" onClick={() => actions.setVision(null)}>Stop sharing</button>
          </>
        ) : vision.state === "starting" ? (
          <>
            <span className="grow" />
            <button type="button" className="btn-secondary compact" onClick={() => actions.setVision(null)}>Cancel</button>
          </>
        ) : (
          <>
            <span className="grow" />
            {caps.camera && <button type="button" className="btn-secondary compact" onClick={() => actions.setVision("camera")}><I.Camera size={16} /> Use camera</button>}
            {caps.screen && <button type="button" className="btn-dark compact" onClick={() => actions.setVision("screen")}><I.Screen size={16} /> {vision.state === "off" ? "Share my screen" : "Try again"}</button>}
          </>
        )}
      </div>
      {open && stream && <Viewer stream={stream} what={what} onCollapse={collapse} />}
    </div>
  );
}

/** The large view: over everything, until Collapse, Esc, or a click outside it. */
function Viewer({ stream, what, onCollapse }: { stream: MediaStream; what: string; onCollapse: () => void }) {
  const close = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    close.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onCollapse();
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [onCollapse]);
  return createPortal(
    <div data-hit className="screen-scrim" onClick={onCollapse}>
      <div role="dialog" aria-modal="true" aria-label={`Your ${what}, as shared with GNSIS`} className="screen-viewer" onClick={(e) => e.stopPropagation()}>
        <header className="screen-bar">
          <span className="live-dot" aria-hidden="true" />
          <span className="grow"><strong>Your {what}</strong><span className="small muted">Shared with GNSIS</span></span>
          <button type="button" className="btn-secondary compact" onClick={() => actions.setVision(null)}>Stop sharing</button>
          <button ref={close} type="button" className="icon-btn" aria-label="Collapse" title="Collapse (Esc)" onClick={onCollapse}><I.Collapse /></button>
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
