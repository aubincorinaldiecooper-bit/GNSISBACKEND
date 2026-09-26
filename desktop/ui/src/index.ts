/**
 * @gnsis/ui — the GNSIS interface, host-agnostic.
 *
 * Mount `GnsisApp` with a `LiveHost` and an `IdentityStore`. The Electron
 * renderer (desktop/src/renderer) and a browser page both do exactly that;
 * nothing in here imports Electron, Node, or a bundler-specific API.
 */
import "blobatar/motion.css";
import "./fonts.css";
import "./styles.css";

export { GnsisApp, type GnsisAppProps } from "./GnsisApp";
export type { HostCapabilities, Identity, IdentityStore, LinkState, LiveEvent, LiveHost, VisionSource } from "./host";
export { LocalIdentityStore } from "./hosts/localIdentity";
export { SimulatedLiveHost } from "./hosts/simulated";
export { deriveId, shareLink } from "./lib/identity";
