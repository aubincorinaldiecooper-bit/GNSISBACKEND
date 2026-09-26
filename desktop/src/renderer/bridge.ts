/**
 * The preload bridge as the renderer sees it (src/preload.ts exposes it as
 * `window.gnsis`). Everything crosses as IPC data; the renderer never holds
 * a socket or an Electron object.
 */
import type { ScreenChannelConfig, ScreenFrameMetadata } from "../shared/protocol.js";

export interface ScreenUpdate {
  channel?: ScreenChannelConfig;
  control?: unknown;
}

export interface GnsisBridge {
  mediaPermissions(): Promise<Record<string, unknown>>;
  requestPermission(kind: string): Promise<string>;
  sendControl(control: unknown): void;
  sendHostEvent(event: unknown): void;
  hostLog(line: string): void;
  startCall(): void;
  /** Close the call on the timeline; the daemon session stays alive. */
  endCall(reason: string): void;
  sendAudioFrame(header: unknown, pcm: Uint8Array): void;
  sendScreenFrame(metadata: ScreenFrameMetadata, payload: Uint8Array): void;
  callTool(tool: string, args: Record<string, unknown>): Promise<unknown>;
  onControl(fn: (control: unknown) => void): void;
  onAudio(fn: (pcm: Uint8Array) => void): void;
  onClosed(fn: (code: number, reason: string) => void): void;
  onScreen(fn: (update: ScreenUpdate) => void): void;
  onInterrupted(fn: () => void): void;
}

declare global {
  interface Window {
    gnsis: GnsisBridge;
  }
}
