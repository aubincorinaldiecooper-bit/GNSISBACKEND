import { useEffect } from "react";
import { TICK_MS } from "./demo/data";
import type { IdentityStore, LiveHost } from "./host";
import { useGesture, useHitRects } from "./lib/platform";
import { actions, configure, loadIdentity, setState, tick, useStore } from "./store/store";
import { Setup } from "./components/Setup";
import { Stage } from "./components/Stage";

export interface GnsisAppProps {
  /** Microphone, speaker, screen, camera and the runtime link — whatever hosts the UI. */
  host: LiveHost;
  /** Where the device identity is kept. */
  identity: IdentityStore;
  /** Fill the dock with sample agents so every state can be reviewed. */
  demo?: boolean;
}

/**
 * The whole GNSIS interface: setup on first run, then the dock, the bar, the
 * chats and the agent panels. Mount it once and hand it a host.
 */
export function GnsisApp({ host, identity, demo = false }: GnsisAppProps) {
  const phase = useStore((s) => s.phase);
  const overlay = useStore((s) => s.caps.overlay);

  useEffect(() => {
    configure(host, identity);
    setState({ demo });
    void loadIdentity();
    const timer = window.setInterval(tick, TICK_MS);
    return () => window.clearInterval(timer);
  }, [host, identity, demo]);

  // In an ordinary window nothing shows through the glass, so draw a desktop.
  useEffect(() => {
    document.body.classList.toggle("gnsis-backdrop", !overlay);
  }, [overlay]);

  useHitRects(host);
  useGesture(actions.toggleLive);

  if (phase === "loading") return null;
  if (phase !== "desktop") return <div className="setup-stage"><Setup /></div>;
  return <Stage />;
}
