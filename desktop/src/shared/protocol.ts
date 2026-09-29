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

/**
 * The header main forwards for a microphone frame, rebuilt from its numbers.
 *
 * The renderer's object is never sent on as it is: the runtime reads any JSON
 * text frame as a control, so passing it through would let the renderer send
 * the runtime any control it liked with a frame of audio attached. Anything
 * that is not a well-formed frame header is refused.
 */
export function audioFrameHeader(value: unknown): AudioFrameHeader | null {
  if (!value || typeof value !== "object") return null;
  const v = value as Record<string, unknown>;
  const count = (k: string) => (Number.isSafeInteger(v[k]) && (v[k] as number) >= 0 ? (v[k] as number) : null);
  const sequence = count("sequence");
  const start = count("start_sample");
  const samples = count("sample_count");
  const at = count("captured_at_ms");
  if (v.type !== "audio.frame" || sequence === null || start === null || samples === null || at === null) return null;
  return { type: "audio.frame", sequence, start_sample: start, sample_count: samples, captured_at_ms: at };
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

/**
 * Screen-channel config the daemon publishes in `ready` and `media.mode.done`
 * (online_duplex.py `_screen_channel`). The screen socket requires `token` —
 * connecting without it is rejected before `screen.ready`.
 */
export interface ScreenChannelConfig {
  enabled?: boolean;
  path?: string | null;
  token?: string | null;
  recommended_frame_rate?: number;
  codex_frame_rate?: number;
  codex_screen_history_seconds?: number;
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
  | { type: "host.event"; event: Record<string, unknown> }
  // The answer to the runtime's `tool.call` with the same call_id, and a
  // keep-alive while the person is still deciding whether to allow it.
  | { type: "tool.response"; call_id: string; content: Record<string, unknown> }
  | { type: "tool.progress"; call_id: string; state: string }
  // The person's own words, transcribed from this machine's microphone.
  | {
      type: "turn.final";
      turn_id: string;
      text: string;
      start_ms: number;
      end_ms: number;
      timestamp_ms: number;
      timezone: string;
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
