/**
 * Browser preview of the UI with the simulated host: no microphone, no
 * runtime, a scripted live conversation. For design review and for the
 * headless checks; never the host of a real build.
 *
 *   npm run ui:preview   (from desktop/)  → dist/ui-preview/, serve it over http
 *   ?demo                fills the dock with the sample agents
 *   ?mac                 the host claims only what the Mac app can do today:
 *                        no typing, no words from speech, screen and camera,
 *                        floating over the desktop
 *   ?overlay             floating over the desktop, with the other defaults
 *
 * Floating, the page draws a stand-in desktop behind the cards (dev only,
 * labelled as such) and turns off glass blur, which a see-through window
 * cannot apply to what is behind it.
 */
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { GnsisApp, LocalIdentityStore, SimulatedLiveHost } from "../src/index";
import "./desktop.css";

const params = new URLSearchParams(location.search);
const mac = params.has("mac");
const overlay = mac || params.has("overlay");
const host = new SimulatedLiveHost(
  mac ? { text: false, transcript: false, screen: true, camera: true, overlay: true } : { overlay },
);
if (overlay) standInDesktop();
const identity = new LocalIdentityStore();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <GnsisApp host={host} identity={identity} demo={params.has("demo")} />
  </StrictMode>,
);

/** Two plain app windows on a wallpaper, and a label saying they are a stand-in. */
function standInDesktop() {
  document.documentElement.classList.add("preview-desktop");
  const apps = document.createElement("div");
  apps.className = "preview-apps";
  apps.setAttribute("aria-hidden", "true");
  const lines = (n: number) => Array.from({ length: n }, (_, i) => `<b style="width:${60 + ((i * 37) % 38)}%"></b>`).join("");
  apps.innerHTML = `
    <div class="preview-app" style="left:4%;top:6%;width:38%;height:52%">
      <div class="preview-app-bar"><i></i><i></i><i></i>Notes</div>
      <div class="preview-app-body"><h3>Groceries</h3>${lines(9)}</div>
    </div>
    <div class="preview-app" style="left:47%;top:4%;width:49%;height:62%">
      <div class="preview-app-bar"><i></i><i></i><i></i>Safari</div>
      <div class="preview-app-body"><h3>Weekend trip ideas</h3>${lines(12)}</div>
    </div>
    <div class="preview-label">Preview: a stand-in desktop behind GNSIS</div>`;
  document.body.prepend(apps);
}
