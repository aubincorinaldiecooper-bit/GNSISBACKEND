import type { HostCapabilities, LiveEvent, LiveHost, VisionSource } from "../host";
import { liveScript, type LiveSegment } from "../demo/data";

type Listener = (e: LiveEvent) => void;

/**
 * A host with no microphone and no runtime: it plays a scripted live
 * conversation so the interface can be reviewed in a plain browser and driven
 * in tests. Every event it emits has the same shape a real host would send;
 * only the source is made up. Never ship this as the host of a real build.
 */
export class SimulatedLiveHost implements LiveHost {
  readonly kind = "simulated";
  private readonly listeners = new Set<Listener>();
  private timers: Array<ReturnType<typeof setTimeout>> = [];
  private muted = false;
  private readonly script: () => LiveSegment[];
  private readonly caps: HostCapabilities;
  /** Time scale: 1 = the script's own pace; tests use a small number. */
  private readonly tenth: number;

  /**
   * `text`, `transcript`, `screen`, `camera`, `overlay`: what it claims it can do. The
   * defaults suit design review; the preview's `?mac` claims what the Mac
   * app can do today, so the screens show what a person there would see.
   */
  /** Where the preview draws its stand-in menu bar icon, when it has one. */
  private readonly iconAt?: () => { x: number; y: number } | null;
  /** Whether GNSIS is out, as far as the menu bar icon is concerned. */
  private out = true;

  constructor(opts: { script?: () => LiveSegment[]; transcript?: boolean; text?: boolean; screen?: boolean; camera?: boolean; overlay?: boolean; menuBarIcon?: () => { x: number; y: number } | null; tenthMs?: number } = {}) {
    this.iconAt = opts.menuBarIcon;
    this.script = opts.script ?? (() => liveScript(null));
    this.tenth = opts.tenthMs ?? 100;
    this.caps = {
      voice: true,
      text: opts.text ?? true,
      screen: opts.screen ?? false,
      camera: opts.camera ?? false,
      transcript: opts.transcript ?? true,
      overlay: opts.overlay ?? false,
      menuBar: !!opts.menuBarIcon,
    };
  }

  capabilities(): HostCapabilities {
    return this.caps;
  }

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    queueMicrotask(() => listener({ type: "link", state: "ready" }));
    return () => this.listeners.delete(listener);
  }

  /** Like the real host: a moment of connecting before the microphone is on. */
  async startLive(): Promise<void> {
    this.clear();
    this.muted = false;
    const connect = this.tenth * 14;
    this.emit({ type: "link", state: "ready" });
    this.at(connect, () => this.emit({ type: "mic", state: "on" }));
    for (const seg of this.script()) this.play(seg, connect);
  }

  /** The preview's stand-in menu bar icon was clicked. */
  pressMenuBarIcon(): void {
    this.out = !this.out;
    this.emit({ type: "menubar", want: this.out ? "show" : "hide" });
  }

  hideToMenuBar(): void {
    this.out = false;
  }

  menuBarIcon(): { x: number; y: number } | null {
    return this.iconAt?.() ?? null;
  }

  async endLive(): Promise<void> {
    this.clear();
    this.emit({ type: "agent.speaking", speaking: false });
    this.emit({ type: "mic", state: "off" });
  }

  async setMuted(muted: boolean): Promise<void> {
    this.muted = muted;
    this.emit({ type: "user.level", level: 0 });
  }

  /** Typed messages reach it, but it has no model: it says so, the way a real reply would arrive. */
  async sendText(_text: string): Promise<void> {
    this.at(this.tenth * 3, () =>
      this.emit({
        type: "agent.text",
        text: "This is the simulated host. Your message reached it, but it has no model to answer with.",
        endOfTurn: true,
        interrupted: false,
      }),
    );
  }

  /**
   * There is no screen to share here, so it shares a drawn picture that says,
   * in large type, that it is simulated: enough to review the screen view in
   * a browser, never mistakable for a person's real screen.
   */
  async startVision(source: VisionSource): Promise<void> {
    if (typeof document === "undefined") throw new Error("The simulated host has no visual sense here.");
    this.stopPicture();
    this.emit({ type: "vision", source, state: "starting" });
    const canvas = document.createElement("canvas");
    canvas.width = 1280;
    canvas.height = 800;
    const g = canvas.getContext("2d")!;
    let frame = 0;
    const draw = () => drawSimulatedScreen(g, canvas.width, canvas.height, source, frame++);
    draw();
    this.pictureTimer = setInterval(draw, 100);
    this.picture = canvas.captureStream(10);
    this.pictureOn = setTimeout(() => this.emit({ type: "vision", source, state: "on" }), this.tenth * 12);
  }

  visionStream(): MediaStream | null {
    return this.picture;
  }

  async stopVision(): Promise<void> {
    const was = this.picture !== null;
    this.stopPicture();
    if (was) this.emit({ type: "vision", source: null, state: "off" });
  }

  private picture: MediaStream | null = null;
  private pictureTimer: ReturnType<typeof setInterval> | null = null;
  private pictureOn: ReturnType<typeof setTimeout> | null = null;

  private stopPicture() {
    if (this.pictureTimer) clearInterval(this.pictureTimer);
    if (this.pictureOn) clearTimeout(this.pictureOn);
    this.pictureTimer = this.pictureOn = null;
    for (const t of this.picture?.getTracks() ?? []) t.stop();
    this.picture = null;
  }

  private play(seg: LiveSegment, offset = 0) {
    if (seg.who === "action") {
      const { state, text } = seg;
      this.at(offset + seg.a * this.tenth, () => this.emit({ type: "action", state, text }));
      return;
    }
    const start = offset + seg.a * this.tenth;
    const end = offset + (seg.cut ?? seg.b) * this.tenth;
    const words = seg.text.split(" ");
    if (seg.who === "agent") {
      const pieces = Math.max(1, Math.ceil(words.length / 3));
      const step = (offset + seg.b * this.tenth - start) / pieces;
      this.at(start, () => this.emit({ type: "agent.speaking", speaking: true }));
      for (let i = 0; i < pieces; i++) {
        const when = start + i * step;
        if (when >= end && seg.cut) break;
        const text = (i ? " " : "") + words.slice(i * 3, i * 3 + 3).join(" ");
        const last = i === pieces - 1 && !seg.cut;
        this.at(when, () => this.emit({ type: "agent.text", text, endOfTurn: last, interrupted: false }));
      }
      for (let t = start; t < end; t += this.tenth) {
        this.at(t, () => this.emit({ type: "agent.level", level: 0.25 + 0.75 * Math.abs(Math.sin(t / this.tenth * 1.7)) }));
      }
      this.at(end, () => {
        if (seg.cut) this.emit({ type: "agent.cut", reason: "user_spoke" });
        this.emit({ type: "agent.speaking", speaking: false });
        this.emit({ type: "agent.level", level: 0 });
      });
      return;
    }
    // The person talks: the microphone hears energy; words arrive if the host can transcribe.
    this.at(start, () => {
      if (this.muted) return;
      this.emit({ type: "user.speech", state: "start" });
    });
    const step = (end - start) / words.length;
    for (let i = 0; i < words.length; i++) {
      this.at(start + i * step, () => {
        if (this.muted) return;
        this.emit({ type: "user.level", level: 0.3 + 0.7 * Math.abs(Math.cos(i * 0.9)) });
        if (this.caps.transcript) this.emit({ type: "user.words", text: words.slice(0, i + 1).join(" "), final: false });
      });
    }
    this.at(end, () => {
      if (this.muted) return;
      if (this.caps.transcript) this.emit({ type: "user.words", text: seg.text, final: true });
      this.emit({ type: "user.level", level: 0 });
      this.emit({ type: "user.speech", state: "end", ms: end - start });
    });
  }

  private at(ms: number, fn: () => void) {
    this.timers.push(setTimeout(fn, Math.max(0, ms)));
  }

  private clear() {
    for (const t of this.timers) clearTimeout(t);
    this.timers = [];
  }

  private emit(e: LiveEvent) {
    for (const l of this.listeners) l(e);
  }
}

/** A plain stand-in desktop with a label no one could take for the real thing. */
function drawSimulatedScreen(g: CanvasRenderingContext2D, w: number, h: number, source: VisionSource, frame: number) {
  const sky = g.createLinearGradient(0, 0, w, h);
  sky.addColorStop(0, "#3b4a6b");
  sky.addColorStop(1, "#8a6f8f");
  g.fillStyle = sky;
  g.fillRect(0, 0, w, h);
  g.fillStyle = "rgba(20,24,33,0.55)";
  g.fillRect(0, 0, w, 28);
  const win = (x: number, y: number, ww: number, hh: number) => {
    g.fillStyle = "rgba(255,255,255,0.92)";
    g.beginPath();
    g.roundRect(x, y, ww, hh, 14);
    g.fill();
    g.fillStyle = "rgba(20,24,33,0.08)";
    g.fillRect(x, y + 36, ww, 1);
    ["#ff5f57", "#febc2e", "#28c840"].forEach((c, i) => {
      g.fillStyle = c;
      g.beginPath();
      g.arc(x + 20 + i * 20, y + 18, 6, 0, Math.PI * 2);
      g.fill();
    });
  };
  win(90, 90, 640, 420);
  win(560, 260, 620, 440);
  g.fillStyle = "#141821";
  g.textAlign = "center";
  g.font = "700 64px system-ui, sans-serif";
  g.fillText(`Simulated ${source}`, w / 2, h / 2 + 10);
  g.font = "400 30px system-ui, sans-serif";
  g.fillStyle = "rgba(20,24,33,0.7)";
  g.fillText(`In the Mac app, your real ${source} appears here.`, w / 2, h / 2 + 60);
  // A cursor that drifts, so the picture visibly moves like a live one.
  const t = frame / 20;
  const cx = w / 2 + Math.cos(t) * 260;
  const cy = h / 2 + 150 + Math.sin(t * 1.3) * 60;
  g.fillStyle = "#141821";
  g.strokeStyle = "#ffffff";
  g.lineWidth = 2;
  g.beginPath();
  g.moveTo(cx, cy);
  g.lineTo(cx + 22, cy + 11);
  g.lineTo(cx + 12, cy + 14);
  g.lineTo(cx + 7, cy + 24);
  g.closePath();
  g.fill();
  g.stroke();
}
