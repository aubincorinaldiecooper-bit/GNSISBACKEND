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

  constructor(opts: { script?: () => LiveSegment[]; transcript?: boolean; tenthMs?: number } = {}) {
    this.script = opts.script ?? (() => liveScript(null));
    this.tenth = opts.tenthMs ?? 100;
    this.caps = { voice: true, text: true, screen: false, camera: false, transcript: opts.transcript ?? true, overlay: false };
  }

  capabilities(): HostCapabilities {
    return this.caps;
  }

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    queueMicrotask(() => listener({ type: "link", state: "ready" }));
    return () => this.listeners.delete(listener);
  }

  async startLive(): Promise<void> {
    this.clear();
    this.muted = false;
    this.emit({ type: "link", state: "ready" });
    this.emit({ type: "mic", state: "on" });
    for (const seg of this.script()) this.play(seg);
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

  async startVision(_source: VisionSource): Promise<void> {
    throw new Error("The simulated host has no visual sense.");
  }

  async stopVision(): Promise<void> {}

  private play(seg: LiveSegment) {
    const start = seg.a * this.tenth;
    const end = (seg.cut ?? seg.b) * this.tenth;
    const words = seg.text.split(" ");
    if (seg.who === "agent") {
      const pieces = Math.max(1, Math.ceil(words.length / 3));
      const step = (seg.b * this.tenth - start) / pieces;
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
