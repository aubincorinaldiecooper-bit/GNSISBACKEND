import { useEffect, useState } from "react";
import { useStore } from "../store/store";
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

/**
 * Everything floats at the bottom of the screen. The chat sits left of centre
 * with the agent panel on its right; with nothing open only the dock shows.
 * On an overlay host the space around these is see-through desktop; in an
 * ordinary window the app draws a backdrop behind them.
 */
export function Stage() {
  const s = useStore((x) => x);
  const { w, h } = useViewport();
  const bar = s.mode === "bar";
  const winOpen = bar && s.winOpen && !!s.convs[s.active];
  const showPanel = winOpen && !s.panelHidden;
  const wide = w >= 1380;
  const panelW = wide ? 600 : 520;
  const stackW = showPanel ? (wide ? 640 : 580) : 720;
  const groupW = showPanel ? stackW + 40 + panelW : stackW;
  const left = Math.max(24, Math.round((w - groupW) / 2));
  const panelH = Math.min(642, h - BOTTOM - 48);
  const winH = panelH - BAR_H - GAP;
  const dockW = dockGeometry(s).width;
  const shellW = bar ? stackW : dockW;
  const shellLeft = bar ? left : Math.round((w - dockW) / 2);
  const showCommands = bar && s.text.startsWith("/") && !s.text.includes(" ");

  return (
    <div className="desktop">
      {showPanel && <AgentPanel left={left + stackW + 40} width={panelW} height={panelH} />}
      <div className="stack" style={{ left, width: stackW, bottom: BOTTOM + BAR_H + GAP }}>
        {winOpen && !showCommands && <ChatWindow height={showPanel ? winH : "auto"} />}
        {showCommands && <Commands />}
      </div>
      <Shell g={{ shellLeft, shellW, shellH: bar ? BAR_H : DOCK_H }} />
      {!bar && s.dockMenu && <DockMenu left={shellLeft + shellW - 300} />}
      {bar && s.visionMenu && <VisionMenu left={shellLeft + 52} />}
      {!bar && s.greet && !s.dockMenu && <Greeting left={shellLeft + 7} />}
      {bar && s.toast && s.toast.id !== s.active && <Toast left={shellLeft + shellW - 380} />}
      {s.settingsOpen && <Settings />}
    </div>
  );
}
