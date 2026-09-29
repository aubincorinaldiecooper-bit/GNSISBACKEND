import { useEffect, useState } from "react";
import { useStore, viewing } from "../store/store";
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
}

/**
 * Where things go. The chat's place depends on the window only, never on the
 * Activity drawer, so opening or closing the drawer moves nothing. The chat
 * is centred when the window is wide enough to keep room for the drawer
 * beside it; otherwise it sits as far left of centre as that room needs, and
 * on the narrowest windows it is a little narrower too. The drawer comes in
 * at the right edge and fills the room beside the chat, never overlapping it.
 */
export function stageLayout(w: number, h: number): StageLayout {
  const chatW = Math.max(0, Math.min(CHAT_W, w - 2 * EDGE - DRAWER_GAP - DRAWER_MIN));
  const chatLeft = Math.max(EDGE, Math.min(Math.round((w - chatW) / 2), w - EDGE - DRAWER_MIN - DRAWER_GAP - chatW));
  const room = w - EDGE - (chatLeft + chatW) - DRAWER_GAP;
  const drawerW = Math.min(DRAWER_MAX, room);
  return { chatLeft, chatW, drawerLeft: w - EDGE - drawerW, drawerW, drawerH: Math.min(642, h - BOTTOM - 48) };
}

/**
 * Everything floats at the bottom of the screen: the chat in the middle, the
 * Activity drawer at the right when it is open; with nothing open only the
 * dock shows. On an overlay host the space around these is see-through
 * desktop; in an ordinary window the app draws a backdrop behind them.
 */
export function Stage() {
  const s = useStore((x) => x);
  const { w, h } = useViewport();
  const bar = s.mode === "bar";
  const winOpen = bar && s.winOpen && !!s.convs[s.active];
  const showPanel = winOpen && !s.panelHidden;
  const { chatLeft: left, chatW: stackW, drawerLeft, drawerW, drawerH } = stageLayout(w, h);
  const dockW = dockGeometry(s).width;
  const shellW = bar ? stackW : dockW;
  const shellLeft = bar ? left : Math.round((w - dockW) / 2);
  const showCommands = bar && s.text.startsWith("/") && !s.text.includes(" ");

  return (
    <div className="desktop">
      <div className="stack" style={{ left, width: stackW, bottom: BOTTOM + BAR_H + GAP }}>
        {winOpen && !showCommands && <ChatWindow height="auto" />}
        {showCommands && <Commands />}
      </div>
      {showPanel && <AgentPanel left={drawerLeft} width={drawerW} height={drawerH} />}
      <Shell g={{ shellLeft, shellW, shellH: bar ? BAR_H : DOCK_H }} />
      {!bar && s.dockMenu && <DockMenu left={shellLeft + shellW - 300} />}
      {bar && s.visionMenu && <VisionMenu left={shellLeft + 52} />}
      {!bar && s.greet && !s.dockMenu && <Greeting left={shellLeft + 7} />}
      {bar && s.toast && !viewing(s, s.toast.id) && <Toast left={shellLeft + shellW - 380} />}
      {s.settingsOpen && <Settings />}
    </div>
  );
}
