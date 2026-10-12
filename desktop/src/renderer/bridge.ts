/**
 * The preload bridge as the renderer sees it (src/preload.ts exposes it as
 * `window.gnsis`). Everything crosses as IPC data; the renderer never holds
 * a socket or an Electron object.
 */
import type { ScreenChannelConfig, ScreenFrameMetadata } from "../shared/protocol.js";
import type { GnsisModel } from "@gnsis/ui";

export interface ScreenUpdate {
  channel?: ScreenChannelConfig;
  control?: unknown;
}

export interface LinkState {
  /** The last `ready` control the daemon sent on this connection, if any. */
  ready: unknown;
  connected: boolean;
  /** The duplex socket closed since it last opened. */
  closed: boolean;
  /** This signed desktop can answer a narrow set of deterministic turns itself. */
  local_turns?: boolean;
}

export interface VoiceProfile {
  id: string;
  name: string;
  description: string;
  kind: "preset" | "custom";
}

export interface VoicePreferences {
  person_id: string;
  persona_description: string;
  voice_profile_id: string;
  voice_pace: "relaxed" | "normal" | "quick";
  available_voice_profiles: VoiceProfile[];
  available_voice_paces: Array<"relaxed" | "normal" | "quick">;
  can_upload_voice: boolean;
}

export interface GnsisBridge {
  /** Backend used for desktop identity enrollment and grant renewal. */
  readonly apiUrl?: string;
  /** GNSIS floats over the desktop in a see-through window. Missing means an ordinary window. */
  readonly overlay?: boolean;
  /** Where the page's cards are, in page pixels, so clicks elsewhere fall through. */
  reportHitRects?(rects: Array<[number, number, number, number]>): void;
  /** GNSIS has an icon in the Mac menu bar to tuck into. Missing means it has none. */
  readonly menuBar?: boolean;
  /** GNSIS's face for the menu bar icon, as a PNG data URL (black on transparent; macOS tints it). */
  setMenuBarFace?(png: string): void;
  /** GNSIS has tucked itself away: hide the window until the icon is clicked. */
  hideToMenuBar?(): void;
  /** The icon was clicked (`want`), or where it is changed (`at`, page pixels from the window's top left). */
  onMenuBar?(fn: (message: unknown) => void): void;
  /** Close GNSIS entirely ("Quit GNSIS"). */
  quit?(): void;
  /** A native yes/no question; resolves true only for the confirming button. */
  confirm?(message: string, confirmLabel: string): Promise<boolean>;
  mediaPermissions(): Promise<Record<string, unknown>>;
  requestPermission(kind: string): Promise<string>;
  sendControl(control: unknown): void;
  sendHostEvent(event: unknown): void;
  /**
   * What the person typed, sent by the main process as their `turn.final` on
   * its own runtime connection, exactly as their spoken words are, and kept
   * as a trusted turn once the runtime accepts it.
   */
  sendTurn(text: string): Promise<TurnResult>;
  modelPreference?(): Promise<GnsisModel>;
  setModelPreference?(model: GnsisModel): Promise<GnsisModel>;
  hostLog(line: string): void;
  /** What the main process knows about the runtime link right now. */
  linkState(): Promise<LinkState>;
  /** Open the runtime sockets again after they closed. */
  reconnect(): void;
  /** Install the short-lived backend-issued credential used by runtime sockets. */
  setDesktopGrant?(grant: string): Promise<void>;
  /** Store the one-time recovery key using the operating system's encryption. */
  protectDesktopIdentity?(
    approvalGrant: string,
    recoveryKey?: string,
  ): Promise<string>;
  /** Remove the OS-protected identity and stop authenticated sockets. */
  eraseDesktopIdentity?(): Promise<void>;
  voicePreferences?(): Promise<VoicePreferences>;
  updateVoicePreferences?(
    values: Partial<
      Pick<
        VoicePreferences,
        "persona_description" | "voice_profile_id" | "voice_pace"
      >
    >,
  ): Promise<VoicePreferences>;
  /** Upload a reference WAV (base64) as a new private voice; it becomes the selected voice. */
  uploadVoiceProfile?(
    name: string,
    transcript: string,
    audioWavB64: string,
  ): Promise<VoicePreferences>;
  deleteVoiceProfile?(profileId: string): Promise<VoicePreferences>;
  wakeGestureEnabled?(): Promise<boolean>;
  setWakeGestureEnabled?(enabled: boolean): Promise<boolean>;
  startCall(): void;
  /** Close the call on the timeline; the daemon session stays alive. */
  endCall(reason: string): void;
  sendAudioFrame(header: unknown, pcm: Uint8Array): void;
  sendScreenFrame(metadata: ScreenFrameMetadata, payload: Uint8Array): void;
  callTool(tool: string, args: Record<string, unknown>): Promise<unknown>;
  onControl(fn: (control: unknown) => void): void;
  onAudio(fn: (pcm: Uint8Array) => void): void;
  /** GNSIS 0.5 speech rendered on this Mac, 24 kHz PCM16. */
  /** Spoken GNSIS 0.5 replies; `meta.call` marks one that belongs to a live call. */
  onLocalAudio?(fn: (pcm: Uint8Array, meta?: { call?: boolean }) => void): void;
  onClosed(fn: (code: number, reason: string) => void): void;
  onScreen(fn: (update: ScreenUpdate) => void): void;
  onInterrupted(fn: () => void): void;
  /** A tap recognized by the optional supported-MacBook wake gesture. */
  onWakeGesture?(fn: (kind: "single" | "double") => void): void;
  /** What GNSIS is doing or did on this computer (main's ActionBroker). */
  onAction(fn: (update: ActionUpdate) => void): void;
}

export type TurnResult = { ok: true; turnId: string } | { ok: false; reason: string; unconfirmed?: boolean };

export interface ActionUpdate {
  callId: string;
  state: "working" | "waiting" | "done" | "failed" | "declined" | "needs_permission";
  text: string;
}

declare global {
  interface Window {
    gnsis: GnsisBridge;
  }
}
