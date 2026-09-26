/**
 * Browser preview of the UI with the simulated host: no microphone, no
 * runtime, a scripted live conversation. For design review and for the
 * headless checks; never the host of a real build.
 *
 *   npm run ui:preview   (from desktop/)  → dist/ui-preview/, serve it over http
 *   ?demo                fills the dock with the sample agents
 */
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { GnsisApp, LocalIdentityStore, SimulatedLiveHost } from "../src/index";

const params = new URLSearchParams(location.search);
const host = new SimulatedLiveHost();
const identity = new LocalIdentityStore();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <GnsisApp host={host} identity={identity} demo={params.has("demo")} />
  </StrictMode>,
);
