/**
 * GNSIS desktop ↔ runtime wire protocol.
 *
 * Mirrors runtime/gnsis_runtime/gnsis_runtime/online_duplex.py and
 * screen_transport.py — these shapes are the contract, not a suggestion.
 *
 * Two sockets per session:
 *   /ws/duplex — JSON controls + binary PCM16 audio frames.
 *   /ws/screen — metadata + binary encoded images (screen and camera share it).
 */

export interface AudioFrameHeader {
  type: "audio.frame";
  sequence: number;
  start_sample: number;
  sample_count: number;
  captured_at_ms: number;
}

export type VideoSource = "screen" | "camera";

export interface ScreenFrameMetadata {
  type: "screen.frame";
  frame_id: string;
  encoding: "jpeg" | "png";
  video_source: VideoSource;
  captured_at_ms: number;
  width?: number;
  height?: number;
}

export type ClientControl =
  | { type: "ping"; id?: number | string }
  | { type: "stop" }
  | { type: "reset" }
  | { type: "break"; reason?: string }
  | {
      type: "playback.ack";
      playback_id: string;
      phase: "started" | "finished";
      epoch: number;
      source_ts_ms?: number;
    }
  | {
      type: "tool.call";
      tool: string;
      args: Record<string, unknown>;
      call_id: string;
    }
  | AudioFrameHeader;

export type ServerControl =
  | { type: "ready"; session_id: string; [k: string]: unknown }
  | { type: "pong"; id?: number | string }
  | { type: "brain.status"; status: string }
  | { type: "session.done" }
  | { type: "playback.ack.done"; accepted: boolean }
  | { type: "error"; message: string; fatal?: boolean }
  | { type: string; [k: string]: unknown };

export const MIC_SAMPLE_RATE = 16_000;
export const PLAYBACK_SAMPLE_RATE = 24_000;
export const AUDIO_CHUNK_MS = 20;
