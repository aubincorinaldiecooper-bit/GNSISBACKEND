/**
 * Runtime websocket clients — one duplex socket (audio + controls) and one
 * screen socket (metadata + encoded frames). Runs in the main process; the
 * renderer talks to it over IPC so the socket lives exactly one place.
 */
import WebSocket from "ws";
import { EventEmitter } from "node:events";
import type {
  AudioFrameHeader,
  ClientControl,
  ScreenChannelConfig,
  ScreenFrameMetadata,
  ServerControl,
} from "../shared/protocol.js";
import { reconnectDelayMs } from "../shared/frames.js";

export interface DuplexClientOptions {
  url: string;
  sessionId: string;
  headers?: Record<string, string>;
}

export class DuplexClient extends EventEmitter {
  private ws: WebSocket | null = null;
  private outbox: Array<string | Buffer> = [];
  private readonly opts: DuplexClientOptions;

  constructor(opts: DuplexClientOptions) {
    super();
    this.opts = opts;
  }

  get connected(): boolean {
    return this.ws?.readyState === WebSocket.OPEN;
  }

  connect(): void {
    const url = `${this.opts.url}/ws/duplex?session_id=${encodeURIComponent(this.opts.sessionId)}`;
    const ws = new WebSocket(url, { headers: this.opts.headers });
    this.ws = ws;
    ws.on("open", () => {
      for (const item of this.outbox.splice(0)) ws.send(item);
      this.emit("open");
    });
    ws.on("message", (data, isBinary) => {
      if (isBinary) {
        this.emit("audio", data as Buffer);
        return;
      }
      let control: ServerControl;
      try {
        control = JSON.parse(data.toString()) as ServerControl;
      } catch {
        return;
      }
      this.emit("control", control);
    });
    ws.on("close", (code, reason) => this.emit("close", code, reason.toString()));
    ws.on("error", (err) => this.emit("error", err));
  }

  sendControl(control: ClientControl): void {
    this._send(JSON.stringify(control));
  }

  sendAudioFrame(header: AudioFrameHeader, pcm: Buffer): void {
    this._send(JSON.stringify(header));
    this._send(pcm);
  }

  private _send(item: string | Buffer): void {
    if (this.connected) this.ws!.send(item);
    else this.outbox.push(item);
  }

  close(): void {
    this.ws?.close();
    this.ws = null;
  }
}

export interface ScreenClientOptions extends DuplexClientOptions {
  /** Daemon screen-channel config (from `ready`/`media.mode.done`). */
  channel?: ScreenChannelConfig;
}

/**
 * /ws/screen transport. Kept deliberately dumb about capture: reconnect is
 * bounded and happens under a still-active capture session — dropping the
 * socket never reacquires the OS source, and frames are simply not sent
 * while disconnected (no unbounded outbox for stale perception).
 */
export class ScreenClient extends EventEmitter {
  private ws: WebSocket | null = null;
  private config: ScreenChannelConfig;
  private readonly opts: ScreenClientOptions;
  private reconnectAttempt = 0;
  private reconnectStartedAt = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private intentionalClose = false;
  private everConnected = false;

  constructor(opts: ScreenClientOptions) {
    super();
    this.opts = opts;
    this.config = opts.channel ?? {};
  }

  /** Apply the daemon's screen-channel config; reconnects if the token changed. */
  applyChannel(config: ScreenChannelConfig): void {
    const tokenChanged =
      config.token != null && config.token !== this.config.token;
    this.config = { ...this.config, ...config };
    if (tokenChanged && this.everConnected) {
      this.reconnectAttempt = 0;
      this.reconnectStartedAt = 0;
      this._open();
    }
  }

  get connected(): boolean {
    return this.ws?.readyState === WebSocket.OPEN;
  }

  connect(): void {
    this.intentionalClose = false;
    this._open();
  }

  private _open(): void {
    const url = new URL(`${this.opts.url}/ws/screen`);
    url.searchParams.set("session_id", this.opts.sessionId);
    if (this.config.token) url.searchParams.set("token", this.config.token);
    const ws = new WebSocket(url, { headers: this.opts.headers });
    this.ws = ws;
    ws.on("open", () => {
      this.everConnected = true;
      this.reconnectAttempt = 0;
      this.reconnectStartedAt = 0;
    });
    ws.on("message", (data, isBinary) => {
      if (isBinary) return;
      try {
        this.emit("control", JSON.parse(data.toString()));
      } catch {
        /* malformed control — ignore */
      }
    });
    ws.on("close", (code, reason) => {
      this.emit("close", code, reason.toString());
      this._scheduleReconnect();
    });
    ws.on("error", (err) => this.emit("error", err));
  }

  private _scheduleReconnect(): void {
    if (this.intentionalClose) return;
    if (!this.reconnectStartedAt) this.reconnectStartedAt = Date.now();
    const delay = reconnectDelayMs(this.reconnectAttempt, this.reconnectStartedAt);
    if (delay === null) {
      this.emit("reconnect_exhausted");
      return;
    }
    this.reconnectAttempt += 1;
    this.emit("reconnect_scheduled", { attempt: this.reconnectAttempt, delay_ms: delay });
    this.reconnectTimer = setTimeout(() => this._open(), delay);
  }

  sendFrame(metadata: ScreenFrameMetadata, payload: Buffer): void {
    // Stale frames are not queued for later replay: if transport is down the
    // sampler's next tick produces a fresher frame instead.
    if (this.ws?.readyState !== WebSocket.OPEN) return;
    this.ws.send(JSON.stringify(metadata));
    this.ws.send(payload);
  }

  close(): void {
    this.intentionalClose = true;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.ws?.close();
    this.ws = null;
  }
}
