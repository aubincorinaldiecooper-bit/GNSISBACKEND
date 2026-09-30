import { useEffect, useState } from "react";
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

  return (
    <div className="desktop">
      <div className="stack" style={{ left, width: stackW, bottom: BOTTOM + BAR_H + GAP }}>
        {/* News goes above an open chat, never over its newest lines. */}
        {toast && aboveChat && <Toast />}
        {winOpen && !showCommands && <ChatWindow height="auto" />}
        {showCommands && <Commands />}
      </div>
      {showPanel && <AgentPanel left={drawerLeft} width={drawerW} height={drawerH} />}
      <Shell g={{ shellLeft, shellW, shellH: bar ? BAR_H : DOCK_H }} />
      {!bar && s.dockMenu && <DockMenu left={shellLeft + shellW - 300} />}
      {bar && s.visionMenu && <VisionMenu left={shellLeft + 52} />}
      {!bar && s.greet && !s.dockMenu && <Greeting left={shellLeft + 7} />}
      {toast && !aboveChat && <Toast left={shellLeft + shellW - 380} />}
      {s.settingsOpen && <Settings />}
    </div>
  );
}
