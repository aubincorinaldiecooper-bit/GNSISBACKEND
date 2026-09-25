/**
 * Small chassis adapters — the only place Electron APIs may appear.
 *
 * The Host logic (call epochs, playback truth, protocol) depends on these
 * contracts, not on electron modules. Swapping chassis means reimplementing
 * these interfaces, nothing else.
 */

export interface CapturedAudioFrame {
  pcm16: Uint8Array;
  sequence: number;
  start_sample: number;
  captured_at_ms: number;
}

export interface CapturedVideoFrame {
  frame_id: string;
  data: Uint8Array;
  encoding: "jpeg" | "png";
  video_source: "screen" | "camera";
  captured_at_ms: number;
  width: number;
  height: number;
}

export type CaptureSource = { kind: "screen" | "camera"; id?: string };

export interface AudioCaptureAdapter {
  start(): Promise<void>;
  stop(): Promise<void>;
  onFrame(cb: (frame: CapturedAudioFrame) => void): void;
}

export interface VideoCaptureAdapter {
  start(source: CaptureSource): Promise<void>;
  stop(): Promise<void>;
  onFrame(cb: (frame: CapturedVideoFrame) => void): void;
}

export interface PlaybackStarted {
  playback_id: string;
  output_epoch: number;
  ts_ms: number;
}

export interface PlaybackCompleted {
  playback_id: string;
  output_epoch: number;
  ts_ms: number;
}

export interface PlaybackAdapter {
  enqueue(pcm16: Uint8Array, output_epoch: number): Promise<string>;
  cancel(outputEpoch: number, reason: string): Promise<void>;
  onStarted(cb: (e: PlaybackStarted) => void): void;
  onCompleted(cb: (e: PlaybackCompleted) => void): void;
  onCancelled(cb: (e: PlaybackCompleted & { reason: string }) => void): void;
}

export type PermissionKind = "microphone" | "camera" | "screen";
export type PermissionState =
  | "granted"
  | "denied"
  | "not-determined"
  | "revoked"
  | "unknown";

export interface DesktopPermissionAdapter {
  status(kind: PermissionKind): Promise<PermissionState>;
  request(kind: PermissionKind): Promise<PermissionState>;
  onChanged?(cb: (kind: PermissionKind, state: PermissionState) => void): void;
}

export interface ShortcutAdapter {
  register(accelerator: string, cb: () => void): void;
  unregisterAll(): void;
}

export interface NotificationAdapter {
  show(title: string, body: string): void;
}

export interface ScreenshotAdapter {
  capture(source?: string): Promise<Uint8Array>;
}

export interface HostAdapters {
  audioCapture: AudioCaptureAdapter;
  videoCapture: VideoCaptureAdapter;
  playback: PlaybackAdapter;
  permissions: DesktopPermissionAdapter;
  shortcuts: ShortcutAdapter;
  notifications: NotificationAdapter;
  screenshots: ScreenshotAdapter;
}
