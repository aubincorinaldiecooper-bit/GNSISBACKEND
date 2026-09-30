import { useEffect, useLayoutEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import { HOME, keepOnScreen, loadPlace, savePlace, type Box, type Place } from "../lib/place";
import { HIT_RECTS_NOW } from "../lib/platform";
import { hasConversation, useStore, viewing } from "../store/store";
import { AgentPanel } from "./AgentPanel";
import { ChatWindow } from "./ChatWindow";
import { Commands, DockMenu, Greeting, Toast, VisionMenu } from "./Overlays";
import { Settings } from "./Settings";
import { Shell, dockGeometry } from "./Shell";

function useViewport() {
  const [v, setV] = useState({ w: window.innerWidth, h: window.innerHeight });
  useEffect(() => {
    const on = () => setV({ w: window.innerWidth, h: window.innerHeight });
    window.addEventListener("resize", on);
    return () => window.removeEventListener("resize", on);
  }, []);
  return v;
}

const BOTTOM = 32;
const BAR_H = 68;
const DOCK_H = 84;
const GAP = 14;
/** The chat and the bar at their widest. */
export const CHAT_W = 720;
/** The narrowest the chat gets for the sake of keeping the drawer beside it. */
export const CHAT_MIN = 560;
const EDGE = 24;
const DRAWER_GAP = 24;
/** Narrow enough to leave room for the chat, wide enough for an agent's work to be read. */
export const DRAWER_MIN = 440;
const DRAWER_MAX = 600;

export interface StageLayout {
  chatLeft: number;
  chatW: number;
  drawerLeft: number;
  drawerW: number;
  drawerH: number;
  /** Too narrow for both side by side: the open drawer lies over the chat's right side. */
  drawerOver: boolean;
}

/**
 * Where things go. The chat's place depends on the window only, never on the
 * Activity drawer, so opening or closing the drawer moves nothing. The chat
 * is centred when the window is wide enough to keep room for the drawer
 * beside it; otherwise it sits as far left of centre as that room needs, and
 * narrows down to CHAT_MIN if it must. The drawer comes in at the right edge
 * and fills the room beside the chat. Only when even that is too tight does
 * the chat keep a readable width, centred, and the drawer lie over it.
 */
export function stageLayout(w: number, h: number): StageLayout {
  const drawerH = Math.min(642, h - BOTTOM - 48);
  if (w - 2 * EDGE - DRAWER_GAP - DRAWER_MIN < CHAT_MIN) {
    const chatW = Math.max(0, Math.min(CHAT_W, w - 2 * EDGE));
    const drawerW = Math.max(0, Math.min(DRAWER_MIN, w - 2 * EDGE));
    return { chatLeft: Math.round((w - chatW) / 2), chatW, drawerLeft: w - EDGE - drawerW, drawerW, drawerH, drawerOver: true };
  }
  const chatW = Math.min(CHAT_W, w - 2 * EDGE - DRAWER_GAP - DRAWER_MIN);
  const chatLeft = Math.max(EDGE, Math.min(Math.round((w - chatW) / 2), w - EDGE - DRAWER_MIN - DRAWER_GAP - chatW));
  const room = w - EDGE - (chatLeft + chatW) - DRAWER_GAP;
  const drawerW = Math.min(DRAWER_MAX, room);
  return { chatLeft, chatW, drawerLeft: w - EDGE - drawerW, drawerW, drawerH, drawerOver: false };
}

/** The dock sits on the chat's centre line, so opening the bar grows it in place. */
export function dockLeft(layout: StageLayout, dockW: number, w: number): number {
  const centre = layout.chatLeft + layout.chatW / 2;
  return Math.max(EDGE, Math.min(Math.round(centre - dockW / 2), w - EDGE - dockW));
}

/** A drag starts on an empty part of the bar or dock, on either face, on the chat's top strip or the drawer's header… */
const DRAG_FROM = ".shell, .tabs, .panel-head";
/** …but not on their controls, except the faces, which still open or close the chat when simply clicked. */
const NOT_FROM = "input, textarea, select, a, [contenteditable], button:not(.dock-sun):not(.addr-face)";
/** How far the pointer moves before a press becomes a drag, so a click stays a click. */
const DRAG_SLOP = 5;

/**
 * Where everything in the cluster sits before it is moved: the union of its
 * parts' layout boxes. The bar grows out of the dock in an animation, so its
 * final box is passed in (`shell`) rather than measured half-way.
 */
function naturalBox(el: HTMLElement, shell: Box): Box {
  let box = shell;
  for (const c of Array.from(el.children) as HTMLElement[]) {
    if (c.classList.contains("shell") || !c.offsetWidth || !c.offsetHeight) continue;
    const b = { left: c.offsetLeft, top: c.offsetTop, right: c.offsetLeft + c.offsetWidth, bottom: c.offsetTop + c.offsetHeight };
    box = { left: Math.min(box.left, b.left), top: Math.min(box.top, b.top), right: Math.max(box.right, b.right), bottom: Math.max(box.bottom, b.bottom) };
  }
  return box;
}

/**
 * The person can drag GNSIS (the dock or bar, the chat and the drawer, all
 * together) anywhere on the screen. It stays fully on screen, also when the
 * chat opens or grows, and the place is remembered for the next launch.
 */
function useMoveable(w: number, h: number, shell: Box) {
  const ref = useRef<HTMLDivElement>(null);
  const shellRef = useRef(shell);
  shellRef.current = shell;
  const [want, setWant] = useState<Place>(loadPlace);
  const [shown, setShown] = useState<Place>(want);
  const [dragging, setDragging] = useState(false);
  const [, remeasure] = useState(0);
  const drag = useRef<{ id: number; x: number; y: number; from: Place; k: number; moved: boolean; last: Place } | null>(null);

  // Whatever opened, closed or grew, keep it all on screen.
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el || drag.current?.moved) return;
    const next = keepOnScreen(want, naturalBox(el, shell), w, h);
    if (next.dx !== shown.dx || next.dy !== shown.dy) setShown(next);
  });

  // A part that changed size in an animation is measured again once it has finished.
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const done = (e: TransitionEvent) => {
      if ((e.target as Element).parentElement === el && /^(left|width|height|top|bottom)$/.test(e.propertyName)) remeasure((n) => n + 1);
    };
    el.addEventListener("transitionend", done);
    return () => el.removeEventListener("transitionend", done);
  }, []);

  useEffect(() => {
    const move = (e: PointerEvent) => {
      const d = drag.current;
      const el = ref.current;
      if (!d || !el || e.pointerId !== d.id) return;
      const ddx = (e.clientX - d.x) / d.k;
      const ddy = (e.clientY - d.y) / d.k;
      if (!d.moved) {
        if (Math.hypot(ddx, ddy) < DRAG_SLOP) return;
        d.moved = true;
        setDragging(true);
      }
      e.preventDefault();
      const next = keepOnScreen({ dx: d.from.dx + ddx, dy: d.from.dy + ddy }, naturalBox(el, shellRef.current), w, h);
      d.last = next;
      setWant(next);
      setShown(next);
    };
    const up = (e: PointerEvent) => {
      const d = drag.current;
      if (!d || e.pointerId !== d.id) return;
      drag.current = null;
      if (!d.moved) return;
      setDragging(false);
      savePlace(d.last);
      // The press was a drag, not a click on whatever it started on.
      const swallow = (ev: MouseEvent) => {
        ev.stopPropagation();
        ev.preventDefault();
      };
      window.addEventListener("click", swallow, { capture: true, once: true });
      window.setTimeout(() => window.removeEventListener("click", swallow, { capture: true }), 0);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", up);
    return () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", up);
    };
  }, [w, h]);

  // Floating over the desktop, the host must keep the pointer on GNSIS for the whole drag.
  useEffect(() => {
    window.dispatchEvent(new Event(HIT_RECTS_NOW));
  }, [dragging]);

  const onPointerDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    const el = ref.current;
    if (e.button !== 0 || drag.current || !el) return;
    const t = e.target as Element;
    if (!t.closest(DRAG_FROM) || t.closest(NOT_FROM)) return;
    // The pointer moves in screen pixels; the page may be drawn scaled.
    const k = el.getBoundingClientRect().width / (el.offsetWidth || 1) || 1;
    drag.current = { id: e.pointerId, x: e.clientX, y: e.clientY, from: shown, k, moved: false, last: shown };
  };

  const moved = shown.dx !== HOME.dx || shown.dy !== HOME.dy;
  return {
    ref,
    dragging,
    onPointerDown,
    style: moved ? { transform: `translate(${shown.dx}px, ${shown.dy}px)` } : undefined,
  };
}

/**
 * Everything floats at the bottom of the screen: the chat, the Activity
 * drawer at the right when it is open; with nothing open only the dock shows,
 * on the chat's centre line. On an overlay host the space around these is
 * see-through desktop; in an ordinary window the app draws a backdrop.
 */
export function Stage() {
  const s = useStore((x) => x);
  const { w, h } = useViewport();
  const bar = s.mode === "bar";
  // No chat window above the bar until there is a conversation to show.
  const winOpen = bar && s.winOpen && hasConversation(s, s.active);
  const showPanel = winOpen && !s.panelHidden;
  const layout = stageLayout(w, h);
  const { chatLeft: left, chatW: stackW, drawerLeft, drawerW, drawerH } = layout;
  const dockW = dockGeometry(s).width;
  const shellW = bar ? stackW : dockW;
  const shellLeft = bar ? left : dockLeft(layout, dockW, w);
  const showCommands = bar && s.text.startsWith("/") && !s.text.includes(" ");
  const toast = bar && !!s.toast && !viewing(s, s.toast.id);
  const aboveChat = winOpen || showCommands;
  const shellH = bar ? BAR_H : DOCK_H;
  const move = useMoveable(w, h, { left: shellLeft, right: shellLeft + shellW, top: h - BOTTOM - shellH, bottom: h - BOTTOM });

  return (
    <div className="desktop">
      <div ref={move.ref} className={"cluster" + (move.dragging ? " is-dragging" : "")} style={move.style} onPointerDown={move.onPointerDown}>
        <div className="stack" style={{ left, width: stackW, bottom: BOTTOM + BAR_H + GAP }}>
          {/* News goes above an open chat, never over its newest lines. */}
          {toast && aboveChat && <Toast />}
          {winOpen && !showCommands && <ChatWindow height="auto" />}
          {showCommands && <Commands />}
        </div>
        {showPanel && <AgentPanel left={drawerLeft} width={drawerW} height={drawerH} />}
        <Shell g={{ shellLeft, shellW, shellH }} />
        {!bar && s.dockMenu && <DockMenu left={shellLeft + shellW - 300} />}
        {bar && s.visionMenu && <VisionMenu left={shellLeft + 52} />}
        {!bar && s.greet && !s.dockMenu && <Greeting left={shellLeft + 7} />}
        {toast && !aboveChat && <Toast left={shellLeft + shellW - 380} />}
      </div>
      {move.dragging && <div data-hit className="drag-shield" aria-hidden="true" />}
      {s.settingsOpen && <Settings />}
    </div>
  );
}
