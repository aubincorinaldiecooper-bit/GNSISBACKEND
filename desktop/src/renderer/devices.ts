/**
 * The renderer's devices: speaker, microphone, and the visual sense.
 *
 * This is the Electron implementation of the capture/playback adapters,
 * lifted out of the old debug page unchanged in behaviour: playback chunks
 * chain through PlaybackScheduler and report real start/finish/cancel as
 * HostEvents; the microphone resamples to 16 kHz PCM16 in an AudioWorklet
 * and mute releases capture only; screen and camera share one persistent
 * capture lifecycle paced by the daemon's recommended frame rate. What is
 * new is measurement for the UI (levels, speaking state, first accepted
 * frame) — the daemon sees exactly what it saw before.
 */

import type { ScreenChannelConfig, ScreenFrameMetadata } from "../shared/protocol.js";
import { MicSession } from "../shared/micSession.js";
import { PlaybackScheduler } from "../shared/playbackScheduler.js";
import { FRAME_FIT_PX, FRAME_JPEG_QUALITY, fitWithin, resolveFrameRate } from "../shared/frames.js";
import { CaptureManager } from "../shared/captureLifecycle.js";
import type { GnsisBridge } from "./bridge.js";

export type Log = (line: string) => void;

// ---- playback + ACK truth ---------------------------------------------------

export class Playback {
  private readonly ctx: AudioContext;
  private readonly analyser: AnalyserNode;
  private readonly samples: Uint8Array<ArrayBuffer>;
  private readonly scheduler: PlaybackScheduler;
  private seq = 0;
  private outputEpoch = 0;
  /** Device playback truth for the UI: something is coming out of the speaker. */
  onSpeaking?: (speaking: boolean) => void;

  constructor(private readonly bridge: GnsisBridge) {
    this.ctx = new AudioContext({ sampleRate: 24000 });
    // The level the UI shows is read off the signal actually going to the
    // speaker, not off the chunks as they arrive.
    this.analyser = this.ctx.createAnalyser();
    this.analyser.fftSize = 256;
    this.analyser.connect(this.ctx.destination);
    this.samples = new Uint8Array(this.analyser.fftSize);
    this.scheduler = new PlaybackScheduler(() => this.ctx.currentTime);
  }

  get speaking(): boolean {
    return this.scheduler.scheduledCount > 0;
  }

  /** 0–1, from the waveform at the speaker. */
  level(): number {
    if (!this.speaking) return 0;
    this.analyser.getByteTimeDomainData(this.samples);
    let sum = 0;
    for (const s of this.samples) {
      const v = (s - 128) / 128;
      sum += v * v;
    }
    return Math.min(1, Math.sqrt(sum / this.samples.length) * 3);
  }

  play(pcm: Uint8Array): void {
    const int16 = new Int16Array(pcm.buffer, pcm.byteOffset, pcm.byteLength / 2);
    const buf = this.ctx.createBuffer(1, int16.length, 24000);
    const chan = buf.getChannelData(0);
    for (let i = 0; i < int16.length; i++) chan[i] = int16[i] / 32768;
    const src = this.ctx.createBufferSource();
    src.buffer = buf;
    src.connect(this.analyser);
    const playback_id = `pb-${++this.seq}`;
    // `playback.started` fires at the actual scheduled start, not enqueue time;
    // a cancelled source can never emit `playback.completed`.
    this.scheduler.schedule(src, buf.duration, {
      onStarted: () => {
        this.bridge.sendHostEvent({ type: "playback.started", playback_id, output_epoch: this.outputEpoch, ts_ms: Date.now() });
        this.onSpeaking?.(true);
      },
      onEnded: () => {
        this.bridge.sendHostEvent({ type: "playback.completed", playback_id, output_epoch: this.outputEpoch, ts_ms: Date.now() });
        this.onSpeaking?.(this.speaking);
      },
    });
  }

  /** Stop everything scheduled and tell the daemon it was cancelled. */
  cancel(reason = "user_interrupt"): number {
    const stopped = this.scheduler.cancelAll();
    this.bridge.sendHostEvent({
      type: "playback.cancelled",
      playback_id: `pb-${this.seq}`,
      output_epoch: this.outputEpoch,
      reason,
      ts_ms: Date.now(),
    });
    this.onSpeaking?.(false);
    return stopped;
  }
}

// ---- mic --------------------------------------------------------------------

export class Mic {
  private seq = 0;
  private startSample = 0;
  private readonly session = new MicSession();
  private stream: MediaStream | null = null;
  private device = "";
  /** Microphone level 0–1, measured on the 16 kHz frames that go to the daemon. */
  onLevel?: (level: number) => void;

  constructor(
    private readonly bridge: GnsisBridge,
    private readonly log: Log,
  ) {}

  get active(): boolean {
    return this.session.active;
  }

  /** Open the microphone and start streaming frames. Does not start a call. */
  async start(): Promise<void> {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    this.stream = stream;
    const label = stream.getAudioTracks()[0]?.label ?? "default";
    if (this.device !== label) {
      this.device = label;
      this.bridge.sendHostEvent({ type: "device.changed", device_kind: "microphone", detail: label, ts_ms: Date.now() });
    }
    const ctx = new AudioContext();
    await this.session.start(async () => ({ stream, ctx }));
    await ctx.audioWorklet.addModule("./pcm-worklet.js");
    const src = ctx.createMediaStreamSource(stream);
    const node = new AudioWorkletNode(ctx, "pcm-resampler");
    node.port.onmessage = (e) => {
      const pcm = new Uint8Array(e.data as ArrayBuffer);
      this.bridge.sendAudioFrame(
        {
          type: "audio.frame",
          sequence: ++this.seq,
          start_sample: this.startSample,
          sample_count: pcm.byteLength / 2,
          captured_at_ms: Date.now(),
        },
        pcm,
      );
      this.startSample += pcm.byteLength / 2;
      this.onLevel?.(pcmLevel(pcm));
    };
    src.connect(node);
    node.connect(ctx.destination);
    this.log("mic on");
  }

  /**
   * Mute semantics: release local capture only — never send {"type":"stop"},
   * which would terminate the daemon session and strand later frames on a dead
   * socket while the UI reports "mic on".
   */
  stop(): void {
    if (!this.session.active) return;
    this.session.stop();
    this.stream = null;
    this.log("mic off");
    this.onLevel?.(0);
  }
}

/** RMS of a PCM16 little-endian chunk, scaled so speech reads as 0.3–1. */
export function pcmLevel(pcm: Uint8Array): number {
  const n = pcm.byteLength >> 1;
  if (n === 0) return 0;
  const view = new DataView(pcm.buffer, pcm.byteOffset, n * 2);
  let sum = 0;
  for (let i = 0; i < n; i++) {
    const v = view.getInt16(i * 2, true) / 32768;
    sum += v * v;
  }
  return Math.min(1, Math.sqrt(sum / n) * 4);
}

// ---- screen / camera ---------------------------------------------------------
// Persistent-stream sampling, mirroring production video.js: the MediaStream is
// acquired once per source activation, a hidden <video>+drawImage produces
// frames (no per-tick ImageCapture), the daemon's recommended_frame_rate sets
// the pace, and frames are fitted to the model's contract — never full-res.

const FRAME_FALLBACK_HZ = 1;

export class Vision {
  private channel: ScreenChannelConfig | null = null;
  private readonly capture = new CaptureManager();
  private source: "screen" | "camera" | null = null;
  /** The daemon accepted a frame from the current source. */
  onAccepted?: (source: "screen" | "camera") => void;
  /** The OS ended the capture (the user stopped sharing, unplugged the camera). */
  onEnded?: () => void;

  constructor(
    private readonly bridge: GnsisBridge,
    private readonly log: Log,
  ) {}

  get active(): boolean {
    return this.capture.active;
  }

  get current(): "screen" | "camera" | null {
    return this.capture.active ? this.source : null;
  }

  applyChannel(channel: ScreenChannelConfig): void {
    this.channel = channel;
    this.log(
      `screen channel: rate=${channel.recommended_frame_rate ?? "?"}Hz ` +
        `history=${channel.codex_screen_history_seconds ?? "?"}s`,
    );
  }

  handleScreenControl(control: unknown): void {
    const c = control as { type?: string; reason?: string } | undefined;
    if (c?.type === "screen.frame.dropped") this.log(`frame dropped: ${c.reason ?? "?"}`);
    if (c?.type === "screen.reconnect") {
      const r = c as { attempt?: number; delay_ms?: number };
      this.log(`screen reconnect #${r.attempt} in ${r.delay_ms}ms (capture stays live)`);
    }
    if (c?.type === "screen.frame.accepted" && this.source && this.capture.active) this.onAccepted?.(this.source);
  }

  async share(source: "screen" | "camera"): Promise<void> {
    // Intentional source switch (or re-selection): CaptureManager stops the
    // previous capture session before this acquires — never two streams or
    // two samplers alive at once.
    this.source = source;
    await this.capture.switchTo(async () => {
      const stream =
        source === "camera"
          ? await navigator.mediaDevices.getUserMedia({ video: true })
          : await navigator.mediaDevices.getDisplayMedia({ video: true });
      const track = stream.getVideoTracks()[0];
      this.bridge.sendHostEvent({
        type: "device.changed",
        device_kind: source,
        detail: track.label ?? source,
        ts_ms: Date.now(),
      });
      const video = document.createElement("video");
      video.muted = true;
      video.srcObject = stream;
      await video.play().catch(() => {});

      let stopped = false;
      let captureTimer: ReturnType<typeof setInterval> | null = null;
      let statsTimer: ReturnType<typeof setInterval> | null = null;
      const stop = () => {
        stopped = true;
        if (captureTimer) clearInterval(captureTimer);
        captureTimer = null;
        if (statsTimer) clearInterval(statsTimer);
        statsTimer = null;
        video.srcObject = null;
        for (const t of stream.getTracks()) {
          try {
            t.stop();
          } catch {
            /* already stopped */
          }
        }
      };
      const sessionHandle = { stop };
      // OS-ended capture is a real capture-ending event; a stale onended after
      // a switch must not tear down the newer session.
      track.onended = () => {
        const wasCurrent = this.capture.active;
        this.capture.stopIfCurrent(sessionHandle);
        if (wasCurrent && !this.capture.active) this.onEnded?.();
      };

      const canvas = document.createElement("canvas");
      const c2d = canvas.getContext("2d")!;
      const rateHz = resolveFrameRate(this.channel?.recommended_frame_rate, FRAME_FALLBACK_HZ);
      let framesSent = 0;
      let sending = false;
      let lastBytes = 0;
      const sendFrame = async () => {
        if (stopped || !video.videoWidth || !video.videoHeight || sending) return;
        sending = true;
        try {
          const fit = fitWithin(video.videoWidth, video.videoHeight);
          if (canvas.width !== fit.width || canvas.height !== fit.height) {
            canvas.width = fit.width;
            canvas.height = fit.height;
          }
          c2d.drawImage(video, 0, 0, fit.width, fit.height);
          const blob = await new Promise<Blob | null>((r) => canvas.toBlob(r, "image/jpeg", FRAME_JPEG_QUALITY));
          // Stopped mid-flight: drop the frame rather than pushing a stale one.
          if (!blob || stopped) return;
          const bytes = new Uint8Array(await blob.arrayBuffer());
          this.bridge.sendScreenFrame(
            {
              type: "screen.frame",
              frame_id: crypto.randomUUID(),
              encoding: "jpeg",
              video_source: source,
              captured_at_ms: Date.now(),
              width: canvas.width,
              height: canvas.height,
            },
            bytes,
          );
          lastBytes = bytes.byteLength;
          framesSent += 1;
        } finally {
          sending = false;
        }
      };
      // The daemon rate-gates by capture timestamp, so pacer skew is fine —
      // the timer just needs to be at/above the recommended rate to feed it.
      captureTimer = setInterval(() => void sendFrame(), 1000 / rateHz);
      this.log(`${source} on: ${video.videoWidth || "?"}x${video.videoHeight || "?"} → ≤${FRAME_FIT_PX}px @ ${rateHz}Hz`);
      statsTimer = setInterval(() => {
        if (framesSent === 0) return;
        if (statsTimer) clearInterval(statsTimer);
        statsTimer = null;
        this.log(`${source} sampling: ${canvas.width}x${canvas.height} · ${(lastBytes / 1024).toFixed(1)}KB/frame · ${rateHz}Hz`);
      }, 2000);
      return sessionHandle;
    });
  }

  stop(): void {
    if (!this.capture.active) return;
    this.capture.stop();
    this.log(`${this.source ?? "capture"} off`);
    this.source = null;
  }
}
