/**
 * HostSession — the chassis-neutral half of the desktop.
 *
 * Owns the runtime sockets and the call/output epoch bookkeeping, and
 * translates neutral HostEvents into the wire controls the daemon accepts
 * (`host.event` for lifecycle, `playback.ack` for playback truth). The daemon
 * never sees an Electron object; a Tauri Host would emit the same events.
 */
import { DuplexClient, ScreenClient } from "../main/wsClient.js";
import {
  HOST_PROTOCOL_VERSION,
  type AudioFrameHeader,
  type HostCapabilities,
  type HostEvent,
} from "./protocol.js";
import type { ScreenFrameMetadata } from "../shared/protocol.js";

export interface HostSessionOptions {
  runtimeUrl: string;
  hostId: string;
  chassis: string;
  capabilities: HostCapabilities;
  onControl?: (control: unknown) => void;
  onAudio?: (pcm: Buffer) => void;
  onClosed?: (code: number) => void;
}

export class HostSession {
  private duplex: DuplexClient | null = null;
  private screen: ScreenClient | null = null;
  private callEpoch = 0;
  private sessionId: string | null = null;
  private readonly opts: HostSessionOptions;

  constructor(opts: HostSessionOptions) {
    this.opts = opts;
  }

  get connected(): boolean {
    return this.duplex?.connected ?? false;
  }

  connect(sessionId: string): void {
    this.sessionId = sessionId;
    this.duplex?.close();
    this.screen?.close();
    this.duplex = new DuplexClient({
      url: this.opts.runtimeUrl,
      sessionId,
    });
    this.screen = new ScreenClient({ url: this.opts.runtimeUrl, sessionId });
    this.duplex.on("control", (c) => this.opts.onControl?.(c));
    this.duplex.on("audio", (pcm) => this.opts.onAudio?.(pcm as Buffer));
    this.duplex.on("close", (code) => {
      this.opts.onClosed?.(code);
      this.emit({ type: "host.disconnected", host_id: this.opts.hostId, reason: `ws_closed_${code}`, ts_ms: Date.now() });
    });
    this.duplex.connect();
    this.screen.connect();
  }

  /** Announce capabilities once the socket is up. */
  ready(): void {
    this.emit({
      type: "host.ready",
      host_id: this.opts.hostId,
      protocol_version: HOST_PROTOCOL_VERSION,
      chassis: this.opts.chassis,
      capabilities: this.opts.capabilities,
      ts_ms: Date.now(),
    });
  }

  startCall(): number {
    this.callEpoch += 1;
    this.emit({
      type: "call.started",
      call_epoch: this.callEpoch,
      session_id: this.sessionId ?? "",
      ts_ms: Date.now(),
    });
    return this.callEpoch;
  }

  endCall(reason: string): void {
    this.emit({
      type: "call.ended",
      call_epoch: this.callEpoch,
      session_id: this.sessionId ?? "",
      reason,
      ts_ms: Date.now(),
    });
    this.duplex?.sendControl({ type: "stop" });
  }

  /** Neutral HostEvent -> wire. playback.* doubles as playback.ack truth. */
  emit(event: HostEvent): void {
    if (event.type === "playback.started" || event.type === "playback.completed") {
      this.duplex?.sendControl({
        type: "playback.ack",
        playback_id: event.playback_id,
        phase: event.type === "playback.started" ? "started" : "finished",
        epoch: event.output_epoch,
        source_ts_ms: event.ts_ms,
      });
    }
    if (event.type === "playback.cancelled") {
      this.duplex?.sendControl({ type: "break", reason: event.reason });
    }
    this.duplex?.sendControl({ type: "host.event", event } as never);
  }

  sendAudioFrame(header: AudioFrameHeader, pcm: Buffer): void {
    this.duplex?.sendAudioFrame(header, pcm);
  }

  sendScreenFrame(metadata: ScreenFrameMetadata, payload: Buffer): void {
    this.screen?.sendFrame(metadata, payload);
  }

  interrupt(reason: string): void {
    this.duplex?.sendControl({ type: "break", reason });
  }

  disconnect(reason: string): void {
    this.emit({
      type: "host.disconnected",
      host_id: this.opts.hostId,
      reason,
      ts_ms: Date.now(),
    });
    this.duplex?.close();
    this.screen?.close();
  }
}
