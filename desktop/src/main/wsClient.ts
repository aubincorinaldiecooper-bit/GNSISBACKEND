/**
 * Runtime websocket clients — one duplex socket (audio + controls) and one
 * screen socket (metadata + encoded frames). Runs in the main process; the
 * renderer talks to it over IPC so the socket lives exactly one place.
 */
import WebSocket from "ws";
import { EventEmitter } from "node:events";
import type { ClientControl, ServerControl } from "../shared/protocol.js";

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

  sendAudioFrame(header: ClientControl, pcm: Buffer): void {
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

export class ScreenClient extends EventEmitter {
  private ws: WebSocket | null = null;
  private readonly opts: DuplexClientOptions;

  constructor(opts: DuplexClientOptions) {
    super();
    this.opts = opts;
  }

  connect(): void {
    const url = `${this.opts.url}/ws/screen?session_id=${encodeURIComponent(this.opts.sessionId)}`;
    const ws = new WebSocket(url, { headers: this.opts.headers });
    this.ws = ws;
    ws.on("close", () => this.emit("close"));
    ws.on("error", (err) => this.emit("error", err));
  }

  sendFrame(metadata: object, payload: Buffer): void {
    if (this.ws?.readyState !== WebSocket.OPEN) return;
    this.ws.send(JSON.stringify(metadata));
    this.ws.send(payload);
  }

  close(): void {
    this.ws?.close();
    this.ws = null;
  }
}
