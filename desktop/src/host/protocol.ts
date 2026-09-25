/**
 * Chassis-neutral Host <-> daemon protocol.
 *
 * The daemon must not know whether the Host is Electron, Tauri, a browser,
 * or another native client — every event is a neutral GNSIS message carrying
 * the IDs/timestamps the shared causal timeline needs. Versioned from the
 * start: host.ready announces capabilities, the daemon answers with its own.
 */

import type {
  AudioFrameHeader,
  ScreenFrameMetadata,
} from "../shared/protocol.js";

export const HOST_PROTOCOL_VERSION = 1;

export interface HostCapabilities {
  mic: boolean;
  camera: boolean;
  screen: boolean;
  playback_ack: boolean;
  global_shortcuts: boolean;
  notifications: boolean;
}

export type HostEvent =
  | {
      type: "host.ready";
      host_id: string;
      protocol_version: number;
      chassis: string;
      capabilities: HostCapabilities;
      ts_ms: number;
    }
  | { type: "host.disconnected"; host_id: string; reason: string; ts_ms: number }
  | {
      type: "call.started";
      call_epoch: number;
      session_id: string;
      ts_ms: number;
    }
  | {
      type: "call.ended";
      call_epoch: number;
      session_id: string;
      reason: string;
      ts_ms: number;
    }
  | {
      type: "device.changed";
      device_kind: "microphone" | "speaker" | "camera" | "screen";
      detail: string;
      ts_ms: number;
    }
  | {
      type: "permission.changed";
      permission: string;
      state: "granted" | "denied" | "not-determined" | "revoked";
      ts_ms: number;
    }
  | {
      type: "playback.started";
      playback_id: string;
      output_epoch: number;
      ts_ms: number;
    }
  | {
      type: "playback.completed";
      playback_id: string;
      output_epoch: number;
      ts_ms: number;
    }
  | {
      type: "playback.cancelled";
      playback_id: string;
      output_epoch: number;
      reason: string;
      ts_ms: number;
    };

/** Neutral media frame headers (payload follows as binary). */
export type { AudioFrameHeader };

/**
 * The daemon's /ws/screen socket accepts exactly one visual-frame type:
 * `screen.frame`, with `video_source` selecting the capture surface. Screen
 * and camera share that contract — `video.frame` is not part of the
 * Host<->daemon wire protocol.
 */
export type VideoFrameHeader = ScreenFrameMetadata;
