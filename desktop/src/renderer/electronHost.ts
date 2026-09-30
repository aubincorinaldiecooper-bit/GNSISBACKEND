/**
 * The Electron host for the GNSIS UI.
 *
 * Sits between the preload bridge (IPC to the main process, which owns the
 * runtime sockets) and the renderer's devices, and turns both into the plain
 * LiveEvents the UI understands. Nothing here decides conversation truth:
 * words come from the runtime's `chunk` controls, speaking from real device
 * playback, interruption from the runtime's `playback.cancel`, the person's
 * turns from microphone energy. Mute releases capture only.
 *
 * Telemetry parity with the old debug page is deliberate: every daemon
 * control still goes to the host log (minus the two ACK echoes), and the mic,
 * capture and screen-channel lines are unchanged, so a real-Mac acceptance run
 * can still be read from <userData>/logs/gnsis-host.log. The log is meant to
 * be shared, so a control goes in without its credentials and without words:
 * see forLog.
 */

import type { HostCapabilities, LinkState, LiveEvent, LiveHost, VisionSource } from "@gnsis/ui";
import type { ScreenChannelConfig } from "../shared/protocol.js";
import type { GnsisBridge, ScreenUpdate, TurnResult } from "./bridge.js";
import type { Log } from "./devices.js";

export interface MicLike {
  readonly active: boolean;
  onLevel?: (level: number) => void;
  start(): Promise<void>;
  stop(): void;
}

export interface PlaybackLike {
  readonly speaking: boolean;
  onSpeaking?: (speaking: boolean) => void;
  level(): number;
  play(pcm: Uint8Array): void;
  cancel(reason?: string): number;
}

export interface VisionLike {
  readonly active: boolean;
  readonly current: VisionSource | null;
  /** The picture being shared, for the person's own view of it. */
  readonly stream?: MediaStream | null;
  onAccepted?: (source: VisionSource) => void;
  onEnded?: () => void;
  applyChannel(channel: ScreenChannelConfig): void;
  handleScreenControl(control: unknown): void;
  share(source: VisionSource): Promise<void>;
  stop(): void;
}

export interface Devices {
  mic: MicLike;
  playback: PlaybackLike;
  vision: VisionLike;
}

export interface ElectronHostOptions {
  /** Microphone energy above this counts as the person talking (0–1). */
  speechThreshold?: number;
  /** Quiet this long ends the person's turn. */
  speechHangoverMs?: number;
  /** How often the speaker level is sampled while a reply plays. */
  levelIntervalMs?: number;
  /**
   * How long starting live voice waits for the runtime to be ready. A cold
   * remote runtime takes about two minutes to load, so the default is
   * generous; the person can end the attempt at any time.
   */
  readyTimeoutMs?: number;
  now?: () => number;
  setInterval?: typeof setInterval;
  clearInterval?: typeof clearInterval;
}

type Wait = "ready" | "aborted" | "timeout";

type Listener = (e: LiveEvent) => void;

export class ElectronLiveHost implements LiveHost {
  readonly kind = "electron";
  private readonly listeners = new Set<Listener>();
  private link: LinkState = "connecting";
  private linkDetail?: string;
  private live = false;
  private muted = false;
  private talking = false;
  private talkingSince = 0;
  private quietSince = 0;
  private lastUserLevelAt = 0;
  private levelTimer: ReturnType<typeof setInterval> | null = null;
  private visionAccepted = false;
  /** The `audio.chunk` header that describes the next binary frame. */
  private pendingAudio: { generation_id?: unknown } | null = null;
  /** The newest generation the daemon has sent audio for. */
  private lastGeneration: number | null = null;
  /** Every generation up to this one is cancelled: its audio is never played. */
  private staleGeneration = -1;
  /** A live start waiting for the runtime; resolved by `ready`, End, or the timeout. */
  private waiting: ((ok: boolean) => void) | null = null;
  /** Bumped by every start and every End: an attempt that no longer matches was cancelled. */
  private attempt = 0;
  /** Resolves once the last start attempt has finished or unwound, so two never hold the microphone. */
  private settling: Promise<void> = Promise.resolve();
  /** The runtime said, in its `ready`, that it answers typed turns. */
  private typedTurns = false;
  /** Where the menu bar icon is, as main last said, so GNSIS shrinks toward it. */
  private iconAt: { x: number; y: number } | null = null;
  private readonly threshold: number;
  private readonly hangoverMs: number;
  private readonly levelIntervalMs: number;
  private readonly readyTimeoutMs: number;
  private readonly now: () => number;
  private readonly setIntervalFn: typeof setInterval;
  private readonly clearIntervalFn: typeof clearInterval;

  constructor(
    private readonly bridge: GnsisBridge,
    private readonly devices: Devices,
    private readonly log: Log,
    opts: ElectronHostOptions = {},
  ) {
    this.threshold = opts.speechThreshold ?? 0.12;
    this.hangoverMs = opts.speechHangoverMs ?? 350;
    this.levelIntervalMs = opts.levelIntervalMs ?? 66;
    this.readyTimeoutMs = opts.readyTimeoutMs ?? 180_000;
    this.now = opts.now ?? (() => Date.now());
    this.setIntervalFn = opts.setInterval ?? setInterval;
    this.clearIntervalFn = opts.clearInterval ?? clearInterval;
  }

  /** Connect to the bridge and the devices. Call once, before the UI mounts. */
  attach(): void {
    const { mic, playback, vision } = this.devices;

    this.bridge.onControl((c) => this.onControl(c));
    this.bridge.onAudio((pcm) => this.onAudio(pcm));
    this.bridge.onClosed((code) => {
      this.log(`duplex closed ${code}`);
      this.setLink("closed", "The connection to GNSIS closed.");
    });
    this.bridge.onScreen((update) => this.onScreen(update));
    this.bridge.onInterrupted(() => {
      this.retireGeneration();
      this.cutPlayback("global_shortcut");
    });
    this.bridge.onAction((update) => {
      this.log(`action ${update.callId} ${update.state}`);
      this.emit({ type: "action", state: update.state, text: update.text });
    });
    this.bridge.onMenuBar?.((m) => this.onMenuBar(m));

    playback.onSpeaking = (speaking) => {
      this.emit({ type: "agent.speaking", speaking });
      if (speaking && !this.levelTimer) {
        this.levelTimer = this.setIntervalFn(() => {
          if (!playback.speaking) return;
          this.emit({ type: "agent.level", level: playback.level() });
        }, this.levelIntervalMs);
      } else if (!speaking && this.levelTimer) {
        this.clearIntervalFn(this.levelTimer);
        this.levelTimer = null;
        this.emit({ type: "agent.level", level: 0 });
      }
    };
    mic.onLevel = (level) => this.onMicLevel(level);
    vision.onAccepted = (source) => {
      if (this.visionAccepted) return;
      this.visionAccepted = true;
      this.log(`${source} accepted by the daemon`);
      this.emit({ type: "vision", source, state: "on" });
    };
    vision.onEnded = () => {
      this.visionAccepted = false;
      this.log("capture ended by the system");
      this.emit({ type: "vision", source: null, state: "off" });
    };

    // The daemon's `ready` may have gone by before this page loaded; the main
    // process kept it. Ask, rather than show "Connecting…" to a runtime that
    // has been ready for a while.
    void this.bridge
      .linkState()
      .then((s) => {
        if (!s || this.link !== "connecting") return;
        if (s.ready && s.connected) {
          this.typedTurns = answersTypedTurns(s.ready);
          this.setLink("ready");
        } else if (s.closed) this.setLink("closed", "The connection to GNSIS closed.");
      })
      .catch(() => {});

    // Permission state is explicit state, reported to the daemon as it is found.
    void this.bridge.mediaPermissions().then((p) => {
      for (const [k, v] of Object.entries(p ?? {})) {
        if (v === "granted" || v === "denied") {
          this.bridge.sendHostEvent({ type: "permission.changed", permission: k, state: v, ts_ms: Date.now() });
        }
      }
    }).catch(() => {});
  }

  capabilities(): HostCapabilities {
    // text: only when the connected runtime says it answers typed turns —
    // a typed turn it merely records would look like Enter doing nothing.
    // transcript: the runtime sends no speech-to-text of the person back.
    // overlay: whether main opened the see-through window over the desktop.
    // menuBar: whether main has an icon in the Mac menu bar to tuck into.
    return {
      voice: true,
      text: this.typedTurns,
      screen: true,
      camera: true,
      transcript: false,
      overlay: this.bridge.overlay === true,
      menuBar: this.bridge.menuBar === true,
    };
  }

  reportHitRects(rects: Array<[number, number, number, number]>): void {
    this.bridge.reportHitRects?.(rects);
  }

  hideToMenuBar(): void {
    this.log("menu bar: tucked away");
    this.bridge.hideToMenuBar?.();
  }

  menuBarIcon(): { x: number; y: number } | null {
    return this.iconAt;
  }

  /** The face arrives as SVG; the menu bar takes a picture, drawn here at twice the icon's 18-point size. */
  menuBarFace(svg: string): void {
    if (!this.bridge.setMenuBarFace || typeof Image === "undefined" || typeof document === "undefined") return;
    const img = new Image();
    img.onload = () => {
      const canvas = document.createElement("canvas");
      canvas.width = canvas.height = 36;
      const g = canvas.getContext("2d");
      if (!g) return;
      g.drawImage(img, 0, 0, 36, 36);
      this.bridge.setMenuBarFace?.(canvas.toDataURL("image/png"));
    };
    img.onerror = () => this.log("menu bar: the face could not be drawn");
    img.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
  }

  /** From main: the icon was clicked (tuck away, or come back), or it moved. */
  private onMenuBar(message: unknown): void {
    const m = message as { want?: unknown; at?: { x?: unknown; y?: unknown } } | null;
    const x = m?.at?.x;
    const y = m?.at?.y;
    if (typeof x === "number" && typeof y === "number" && Number.isFinite(x) && Number.isFinite(y)) this.iconAt = { x, y };
    if (m?.want === "hide" || m?.want === "show") {
      this.log(`menu bar: ${m.want === "hide" ? "tuck away" : "come back"}`);
      this.emit({ type: "menubar", want: m.want });
    }
  }

  async confirm(message: string, confirmLabel: string): Promise<boolean> {
    if (!this.bridge.confirm) return window.confirm(message);
    return (await this.bridge.confirm(message, confirmLabel)) === true;
  }

  async sendText(text: string): Promise<void> {
    const words = text.trim();
    if (!words) throw new Error("There is nothing to send.");
    if (!this.typedTurns) throw new Error("Typing isn’t connected to GNSIS yet.");
    this.log(`typed turn requested (${words.length} chars)`);
    const result: TurnResult = await this.bridge.sendTurn(words).catch((e: unknown) => ({
      ok: false as const,
      reason: e instanceof Error && e.message ? e.message : "The message could not reach GNSIS.",
    }));
    if (!result.ok) {
      this.log(`typed turn failed: ${result.reason}`);
      // The UI says "not sent" only for a message that never went out.
      throw Object.assign(new Error(result.reason), { unconfirmed: result.unconfirmed === true });
    }
    this.log(`typed turn ${result.turnId} accepted`);
  }

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    // Read the link at delivery time, not at subscribe time: a `ready` that
    // lands in between must not be followed by a stale "connecting".
    queueMicrotask(() => {
      if (this.listeners.has(listener)) listener({ type: "link", state: this.link, detail: this.linkDetail });
    });
    return () => this.listeners.delete(listener);
  }

  async startLive(): Promise<void> {
    if (this.live || this.waiting) return;
    const attempt = ++this.attempt;
    this.log("live requested");
    // An earlier attempt may still be opening the microphone after End was
    // pressed: let it close again first, so two attempts never share it.
    await this.settling;
    if (attempt !== this.attempt) return;
    let settled!: () => void;
    this.settling = new Promise((resolve) => (settled = resolve));
    try {
      await this.open(attempt);
    } finally {
      settled();
    }
  }

  private async open(attempt: number): Promise<void> {
    // The microphone opens only once the runtime is ready to hear it: frames
    // sent into a connecting socket would queue up and arrive as one stale
    // burst, and a dead socket would leave the UI "listening" to nothing.
    if (this.link !== "ready") {
      if (this.link === "closed" || this.link === "error") {
        this.log("reconnect: live voice requested while the link is down");
        this.setLink("connecting", "Reconnecting to GNSIS.");
        this.bridge.reconnect();
      }
      this.log("live: waiting for the runtime to be ready");
      const outcome = await this.waitForReady();
      if (outcome === "aborted" || attempt !== this.attempt) {
        this.log("live: cancelled while connecting");
        return;
      }
      if (outcome === "timeout") {
        this.log("live: the runtime did not become ready in time");
        throw new Error("GNSIS could not be reached.");
      }
    }
    this.bridge.startCall();
    try {
      await this.devices.mic.start();
    } catch (e) {
      const detail = micProblem(e);
      this.log(`mic failed: ${String(e)}`);
      // The call was opened on the timeline; close it, or its epoch stays open.
      this.bridge.endCall("mic_failed");
      if (attempt !== this.attempt) return;
      this.emit({ type: "mic", state: isDenied(e) ? "denied" : "error", detail });
      throw new Error(detail);
    }
    if (attempt !== this.attempt) {
      // End was pressed while the microphone was opening: it never goes live.
      this.devices.mic.stop();
      this.bridge.endCall("cancelled_while_starting");
      this.log("live: cancelled while the microphone was opening; it is closed again");
      return;
    }
    this.live = true;
    this.muted = false;
    this.log("live on");
    this.emit({ type: "mic", state: "on" });
  }

  async endLive(): Promise<void> {
    // End while still connecting or opening the microphone: that attempt is
    // abandoned, and it closes whatever it had opened on its own.
    this.attempt += 1;
    this.waiting?.(false);
    if (!this.live) return;
    this.live = false;
    this.endSpeech();
    this.devices.mic.stop();
    // Whatever the daemon is still sending for this reply is stale from here:
    // cancel what is scheduled, and refuse what has not arrived yet.
    this.retireGeneration();
    if (this.devices.playback.speaking) this.cutPlayback("live_ended");
    this.bridge.endCall("live_ended");
    this.log("live off");
    this.emit({ type: "mic", state: "off" });
  }

  async setMuted(muted: boolean): Promise<void> {
    if (muted === this.muted) return;
    this.muted = muted;
    if (muted) {
      this.endSpeech();
      this.devices.mic.stop();
      this.emit({ type: "user.level", level: 0 });
      this.log("muted");
      return;
    }
    if (!this.live) return;
    // Muted again, or ended, while the microphone was opening: the stop that
    // came then had nothing to release yet, so it is released as soon as it
    // opens. The whole of that joins `settling`, so a new live start waits
    // for it before opening the microphone itself — and is never the one
    // released by it.
    const opened = this.devices.mic.start().then(() => {
      if (!this.muted && this.live) return true;
      this.devices.mic.stop();
      this.log("unmute abandoned: muted or ended while the microphone opened");
      return false;
    });
    this.settling = this.settling.then(() => opened.then(() => {}, () => {}));
    try {
      if (await opened) this.log("unmuted");
    } catch (e) {
      const detail = micProblem(e);
      this.emit({ type: "mic", state: isDenied(e) ? "denied" : "error", detail });
      throw new Error(detail);
    }
  }

  async startVision(source: VisionSource): Promise<void> {
    this.visionAccepted = false;
    this.emit({ type: "vision", source, state: "starting" });
    try {
      await this.devices.vision.share(source);
    } catch (e) {
      const denied = isDenied(e);
      const detail = visionProblem(e, source);
      this.log(`${source} failed: ${String(e)}`);
      this.emit({ type: "vision", source, state: denied ? "denied" : "error", detail });
      throw new Error(detail);
    }
  }

  visionStream(): MediaStream | null {
    return this.devices.vision.stream ?? null;
  }

  async stopVision(): Promise<void> {
    this.devices.vision.stop();
    this.visionAccepted = false;
    this.emit({ type: "vision", source: null, state: "off" });
  }

  // ---- daemon -> UI ------------------------------------------------------------

  private onControl(control: unknown): void {
    const c = control as { type?: string; [k: string]: unknown } | null;
    const type = c?.type;
    if (type === "playback.ack.done" || type === "host.event.done") return;
    this.log(`<- ${type ?? "?"} ${forLog(control).slice(0, 160)}`);
    switch (type) {
      case "runtime.status":
        if (c?.status === "loading") this.setLink("connecting", "GNSIS is loading itself onto a machine.");
        break;
      case "ready":
        this.typedTurns = answersTypedTurns(c);
        this.setLink("ready");
        break;
      case "session.done":
        this.setLink("closed", "The session ended.");
        break;
      case "audio.done":
        // The last unit of a reply says so; close the reply even if the text
        // chunk that ended it never carried the flag.
        if (c?.end_of_turn) this.emit({ type: "agent.text", text: "", endOfTurn: true, interrupted: false });
        break;
      case "transport.error":
        this.setLink("error", "GNSIS could not be reached.");
        break;
      case "error":
        if (c?.fatal) this.setLink("error", "The session could not start.");
        break;
      case "playback.cancel": {
        // The daemon names the generation it cancelled; anything from it that
        // is still in flight must not play when it lands.
        const cancelled = toGeneration(c?.cancelled_generation_id) ?? this.lastGeneration;
        if (cancelled !== null) this.staleGeneration = Math.max(this.staleGeneration, cancelled);
        this.cutPlayback("daemon_cancel");
        break;
      }
      case "audio.chunk": {
        this.pendingAudio = c as { generation_id?: unknown };
        const gen = toGeneration(c?.generation_id);
        if (gen !== null) this.lastGeneration = Math.max(this.lastGeneration ?? -1, gen);
        break;
      }
      case "chunk": {
        const text = typeof c?.text === "string" ? c.text : "";
        const endOfTurn = !!c?.end_of_turn;
        const interrupted = !!c?.interrupted;
        if (text || endOfTurn || interrupted) this.emit({ type: "agent.text", text, endOfTurn, interrupted });
        break;
      }
      default:
        break;
    }
  }

  /**
   * Binary audio always follows the `audio.chunk` header that describes it.
   * It plays only for a live session and a generation the daemon has not
   * cancelled; anything else is dropped and logged as stale, never scheduled
   * and never acknowledged — so a reply cannot resume after the person ended
   * the conversation or spoke over it.
   */
  private onAudio(pcm: Uint8Array): void {
    const header = this.pendingAudio;
    this.pendingAudio = null;
    const gen = toGeneration(header?.generation_id);
    let reason: string | null = null;
    if (!this.live) reason = "not_live";
    else if (gen !== null && gen <= this.staleGeneration) reason = "cancelled_generation";
    if (reason) {
      this.log(`stale audio dropped gen=${gen ?? "?"} bytes=${pcm.byteLength} reason=${reason}`);
      return;
    }
    this.devices.playback.play(pcm);
  }

  private onScreen(update: ScreenUpdate): void {
    if (update.channel) this.devices.vision.applyChannel(update.channel);
    if (update.control) this.devices.vision.handleScreenControl(update.control);
  }

  private cutPlayback(reason: string): void {
    this.devices.playback.cancel(reason);
    this.emit({ type: "agent.cut", reason });
  }

  /** A local cut: the generation playing now is stale from here on. */
  private retireGeneration(): void {
    if (this.lastGeneration !== null) this.staleGeneration = Math.max(this.staleGeneration, this.lastGeneration);
  }

  private setLink(state: LinkState, detail?: string): void {
    // Typing is only as good as the `ready` that offered it: once that link
    // is gone, it waits for the next one to say so again.
    if (state !== "ready") this.typedTurns = false;
    this.link = state;
    this.linkDetail = detail;
    this.emit({ type: "link", state, detail });
    if (state === "ready") this.waiting?.(true);
    else if (state === "closed" || state === "error") this.waiting?.(false);
  }

  private waitForReady(): Promise<Wait> {
    return new Promise((resolve) => {
      let timer: ReturnType<typeof setTimeout> | null = null;
      const finish = (outcome: Wait) => {
        if (timer) clearTimeout(timer);
        timer = null;
        this.waiting = null;
        resolve(outcome);
      };
      timer = setTimeout(() => finish("timeout"), this.readyTimeoutMs);
      this.waiting = (ok) => finish(ok ? "ready" : "aborted");
    });
  }

  // ---- microphone energy -> the person's turns ----------------------------------

  private onMicLevel(level: number): void {
    if (!this.live || this.muted) return;
    const now = this.now();
    if (now - this.lastUserLevelAt >= 50) {
      this.lastUserLevelAt = now;
      this.emit({ type: "user.level", level });
    }
    if (level >= this.threshold) {
      this.quietSince = 0;
      if (!this.talking) {
        this.talking = true;
        this.talkingSince = now;
        this.emit({ type: "user.speech", state: "start" });
      }
    } else if (this.talking) {
      if (!this.quietSince) this.quietSince = now;
      else if (now - this.quietSince >= this.hangoverMs) this.endSpeech(this.quietSince);
    }
  }

  private endSpeech(at = this.now()): void {
    if (!this.talking) return;
    this.talking = false;
    this.quietSince = 0;
    this.emit({ type: "user.speech", state: "end", ms: Math.max(0, at - this.talkingSince) });
  }

  private emit(e: LiveEvent): void {
    for (const l of this.listeners) l(e);
  }
}

const isDenied = (e: unknown) => (e as { name?: string } | null)?.name === "NotAllowedError";

/**
 * Does this runtime answer typed turns? It says so in its `ready`. A runtime
 * that only records a typed turn (for the action policy and background tasks)
 * never replies to it, so typing stays off rather than seem to do nothing.
 */
/** Fields that carry what someone said, typed or asked for: logged as their length only. */
const WORDS = new Set(["text", "arguments", "content", "transcript", "final_asr"]);

/**
 * A runtime control as the host log keeps it: its shape, ids and flags, with
 * every credential (a resume token, the screen token) removed and every field
 * of words reduced to its length. The log is shared after a test run; it must
 * not hand over the session or repeat what the person said.
 */
export function forLog(control: unknown): string {
  return JSON.stringify(control, (key, value) => {
    if (/token|secret|password/i.test(key)) return "<redacted>";
    if (WORDS.has(key) && value !== null && value !== undefined) {
      const size = typeof value === "string" ? value.length : JSON.stringify(value).length;
      return `<${size} chars>`;
    }
    return value;
  }) ?? String(control);
}

const answersTypedTurns = (ready: unknown): boolean =>
  (ready as { typed_turns?: unknown } | null)?.typed_turns === true;

const toGeneration = (v: unknown): number | null =>
  typeof v === "number" && Number.isFinite(v) ? v : null;

function micProblem(e: unknown): string {
  const name = (e as { name?: string } | null)?.name;
  if (name === "NotAllowedError") return "The microphone was not allowed. Turn it on for GNSIS in System Settings, Privacy & Security, Microphone.";
  if (name === "NotFoundError") return "No microphone was found.";
  if (name === "NotReadableError") return "The microphone is in use by another app.";
  return "The microphone did not open.";
}

function visionProblem(e: unknown, source: VisionSource): string {
  const name = (e as { name?: string } | null)?.name;
  if (source === "screen") {
    if (name === "NotAllowedError") return "Screen recording was not allowed. Turn it on for GNSIS in System Settings, Privacy & Security, Screen Recording, or choose a window when asked.";
    return "Sharing the screen did not start.";
  }
  if (name === "NotAllowedError") return "The camera was not allowed. Turn it on for GNSIS in System Settings, Privacy & Security, Camera.";
  if (name === "NotFoundError") return "No camera was found.";
  return "The camera did not start.";
}
