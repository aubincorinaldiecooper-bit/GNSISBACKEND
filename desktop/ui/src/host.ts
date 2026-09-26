/**
 * What the UI needs from whatever is hosting it.
 *
 * The UI never touches a microphone, a socket, Electron, or a browser API
 * that only one host has. It asks the host to start or end live voice, to
 * mute, to point the visual sense at the screen or the camera, and it listens
 * to the host's account of what is happening. The Electron renderer, a plain
 * browser page in GNSISFRONTEND, and the simulated host used for design review
 * all implement this same contract, so the components are written once.
 *
 * Every event is what the host actually observed, never what it expects:
 * `agent.text` is text the runtime sent, `agent.speaking` is real device
 * playback, `user.speech` is the microphone's energy. A UI built on these
 * cannot show a reply that was never spoken or a mic that is not really on.
 */

export type VisionSource = "screen" | "camera";

export interface HostCapabilities {
  /** Live speech-to-speech is available at all. */
  voice: boolean;
  /**
   * A typed message reaches GNSIS. Without it, typing gets a plain line saying
   * so, never a made-up reply; the stand-in agents are only shown in demo mode.
   */
  text: boolean;
  /** The host can feed the runtime frames of the screen. */
  screen: boolean;
  /** The host can feed the runtime frames from a camera. */
  camera: boolean;
  /**
   * The user's own words come back as text (`user.words`). Without it, spoken
   * turns are shown as spoken markers with a duration, never invented words,
   * and dictation is not offered.
   */
  transcript: boolean;
  /**
   * The UI floats over the desktop on a transparent window. Off means it is
   * inside an ordinary window and draws its own backdrop.
   */
  overlay: boolean;
}

export type LinkState = "connecting" | "ready" | "closed" | "error";

export type LiveEvent =
  /** The connection to the runtime, as the host sees it. */
  | { type: "link"; state: LinkState; detail?: string }
  /** Words the runtime is saying, in the order it says them. */
  | { type: "agent.text"; text: string; endOfTurn: boolean; interrupted: boolean }
  /** Audio is actually coming out of the speaker (device playback truth). */
  | { type: "agent.speaking"; speaking: boolean }
  /** Playback was cancelled before the reply finished: the user spoke over it, or asked. */
  | { type: "agent.cut"; reason: string }
  /** Speaker output level, 0–1. */
  | { type: "agent.level"; level: number }
  /** Microphone input level, 0–1 (0 while muted). */
  | { type: "user.level"; level: number }
  /** The person started or stopped talking, by microphone energy. */
  | { type: "user.speech"; state: "start" | "end"; ms?: number }
  /** The person's words as text, when the host can provide them. */
  | { type: "user.words"; text: string; final: boolean }
  /** Microphone state. `denied` and `error` carry a plain-English detail. */
  | { type: "mic"; state: "on" | "off" | "denied" | "error"; detail?: string }
  /** The visual sense. `on` is sent only once the runtime has accepted a frame. */
  | {
      type: "vision";
      source: VisionSource | null;
      state: "starting" | "on" | "off" | "denied" | "error";
      detail?: string;
    };

export interface LiveHost {
  /** "electron", "browser", "simulated" — for telemetry and tests, never for behaviour branches in components. */
  readonly kind: string;
  capabilities(): HostCapabilities;
  subscribe(listener: (event: LiveEvent) => void): () => void;
  /** Open the microphone and start a call. Resolves once capture is running. */
  startLive(): Promise<void>;
  /** Close the microphone and stop any reply still playing. */
  endLive(): Promise<void>;
  /** Mute releases the microphone only; the session stays alive for unmute. */
  setMuted(muted: boolean): Promise<void>;
  startVision(source: VisionSource): Promise<void>;
  stopVision(): Promise<void>;
  /**
   * For an overlay host: where the interactive surfaces are, in window
   * pixels, so clicks anywhere else can fall through to the desktop.
   */
  reportHitRects?(rects: Array<[number, number, number, number]>): void;
}

export interface Identity {
  /** e.g. gnsis:7FK3-C918-4E2A — derived from the public key, safe to share. */
  publicId: string;
  /** base64 public key */
  publicKey: string;
  /** Where the private key lives. Drives the wording in Settings; never overstated. */
  storage: "keychain" | "local";
}

export interface IdentityStore {
  load(): Promise<Identity | null>;
  create(): Promise<Identity>;
  erase(): Promise<void>;
  /** One plain sentence about where the private key is kept, shown in Settings. */
  readonly storageNote: string;
}
