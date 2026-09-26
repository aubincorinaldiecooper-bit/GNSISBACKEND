/**
 * GNSIS desktop renderer — mounts the product UI over the Electron host.
 *
 * The renderer owns the devices (Electron implementation of the
 * capture/playback adapters) and reports only neutral HostEvents upstream.
 * The interface itself is @gnsis/ui, shared with the web app; this file is
 * the only place that knows it is running in Electron.
 */

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { GnsisApp, LocalIdentityStore } from "@gnsis/ui";
import { Mic, Playback, Vision } from "./devices.js";
import { ElectronLiveHost } from "./electronHost.js";

const bridge = window.gnsis;
const log = (line: string) => {
  try {
    bridge.hostLog(line);
  } catch {
    /* packaged or dev — logging is best-effort */
  }
};

const host = new ElectronLiveHost(
  bridge,
  { mic: new Mic(bridge, log), playback: new Playback(bridge), vision: new Vision(bridge, log) },
  log,
);
host.attach();

const identity = new LocalIdentityStore();
const demo = new URLSearchParams(location.search).has("demo");

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <GnsisApp host={host} identity={identity} demo={demo} />
  </StrictMode>,
);
