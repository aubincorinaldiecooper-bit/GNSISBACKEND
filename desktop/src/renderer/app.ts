/**
 * GNSIS desktop renderer — owns the actual devices (Electron implementation
 * of the capture/playback adapters). It reports only neutral HostEvents
 * upstream: playback.started/completed/cancelled, device.changed,
 * permission.changed. The daemon never sees a DOM or Electron API.
 */

import type {
  ScreenChannelConfig,
  ScreenFrameMetadata,
} from "../shared/protocol.js";
import {
  FRAME_FIT_PX,
  FRAME_JPEG_QUALITY,
  fitWithin,
  resolveFrameRate,
} from "../shared/frames.js";
import { CaptureManager } from "../shared/captureLifecycle.js";

declare const gnsis: {
  mediaPermissions(): Promise<Record<string, unknown>>;
  requestPermission(kind: string): Promise<string>;
  sendControl(control: unknown): void;
  sendHostEvent(event: unknown): void;
  hostLog(line: string): void;
  startCall(): void;
  sendAudioFrame(header: unknown, pcm: Uint8Array): void;
  sendScreenFrame(metadata: ScreenFrameMetadata, payload: Uint8Array): void;
  callTool(tool: string, args: Record<string, unknown>): Promise<unknown>;
  onControl(fn: (control: unknown) => void): void;
  onAudio(fn: (pcm: Uint8Array) => void): void;
  onClosed(fn: (code: number, reason: string) => void): void;
  onScreen(fn: (update: { channel?: ScreenChannelConfig; control?: unknown }) => void): void;
  onInterrupted(fn: () => void): void;
};

const log = (m: string) => {
  const el = document.getElementById("log")!;
  el.textContent += `${new Date().toISOString().slice(11, 19)} ${m}\n`;
  el.scrollTop = el.scrollHeight;
  try {
    gnsis.hostLog(m);
  } catch {
    /* packaged or dev — logging is best-effort */
  }
};
const setStatus = (m: string) =>
  (document.getElementById("status")!.textContent = m);

// ---- playback + ACK truth ---------------------------------------------------

const playbackCtx = new AudioContext({ sampleRate: 24000 });
let playbackSeq = 0;
let outputEpoch = 0;
const scheduledSources = new Set<AudioBufferSourceNode>();
let nextStartTime = 0;

async function playPcm(pcm: Uint8Array): Promise<void> {
  const int16 = new Int16Array(pcm.buffer, pcm.byteOffset, pcm.byteLength / 2);
  const buf = playbackCtx.createBuffer(1, int16.length, 24000);
  const chan = buf.getChannelData(0);
  for (let i = 0; i < int16.length; i++) chan[i] = int16[i] / 32768;
  const src = playbackCtx.createBufferSource();
  src.buffer = buf;
  src.connect(playbackCtx.destination);
  const playback_id = `pb-${++playbackSeq}`;
  gnsis.sendHostEvent({ type: "playback.started", playback_id, output_epoch: outputEpoch, ts_ms: Date.now() });
  src.onended = () => {
    scheduledSources.delete(src);
    gnsis.sendHostEvent({ type: "playback.completed", playback_id, output_epoch: outputEpoch, ts_ms: Date.now() });
  };
  scheduledSources.add(src);
  // Chain chunks instead of overlapping them: each buffer starts when the
  // previous one ends (or now, whichever is later).
  const startAt = Math.max(playbackCtx.currentTime, nextStartTime);
  src.start(startAt);
  nextStartTime = startAt + buf.duration;
}

function interruptPlayback(reason = "user_interrupt"): void {
  for (const src of scheduledSources) {
    try {
      src.stop();
    } catch {
      /* already ended */
    }
  }
  scheduledSources.clear();
  nextStartTime = 0;
  gnsis.sendHostEvent({
    type: "playback.cancelled",
    playback_id: `pb-${playbackSeq}`,
    output_epoch: outputEpoch,
    reason,
    ts_ms: Date.now(),
  });
}

// ---- mic --------------------------------------------------------------------

let seq = 0;
let startSample = 0;
let micStream: MediaStream | null = null;
let micAudioCtx: AudioContext | null = null;
let micDevice = "";

async function startMic(): Promise<void> {
  micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
  const label = micStream.getAudioTracks()[0]?.label ?? "default";
  if (micDevice !== label) {
    micDevice = label;
    gnsis.sendHostEvent({ type: "device.changed", device_kind: "microphone", detail: label, ts_ms: Date.now() });
  }
  gnsis.startCall();
  const ctx = new AudioContext();
  micAudioCtx = ctx;
  await ctx.audioWorklet.addModule("./pcm-worklet.js");
  const src = ctx.createMediaStreamSource(micStream);
  const node = new AudioWorkletNode(ctx, "pcm-resampler");
  node.port.onmessage = (e) => {
    const pcm = new Uint8Array(e.data);
    gnsis.sendAudioFrame(
      {
        type: "audio.frame",
        sequence: ++seq,
        start_sample: startSample,
        sample_count: pcm.byteLength / 2,
        captured_at_ms: Date.now(),
      },
      pcm,
    );
    startSample += pcm.byteLength / 2;
  };
  src.connect(node);
  node.connect(ctx.destination);
  log("mic on");
}

function stopMic(): void {
  // Mute semantics: end local capture only. Sending {"type":"stop"} would
  // terminate the daemon session and close the duplex socket; restarting the
  // mic would then queue frames on a dead transport while the UI reports "on".
  micStream?.getTracks().forEach((t) => t.stop());
  micStream = null;
  void micAudioCtx?.close();
  micAudioCtx = null;
  log("mic off");
}

// ---- screen / camera ---------------------------------------------------------
// Persistent-stream sampling, mirroring production video.js: the MediaStream is
// acquired once per source activation, a hidden <video>+drawImage produces
// frames (no per-tick ImageCapture), the daemon's recommended_frame_rate sets
// the pace, and frames are fitted to the model's contract — never full-res.

let screenChannel: ScreenChannelConfig | null = null;
const capture = new CaptureManager();
const FRAME_FALLBACK_HZ = 1;

gnsis.onScreen((update) => {
  if (update.channel) {
    screenChannel = update.channel;
    log(
      `screen channel: rate=${screenChannel.recommended_frame_rate ?? "?"}Hz ` +
        `history=${screenChannel.codex_screen_history_seconds ?? "?"}s`,
    );
  }
  const c = update.control as { type?: string; reason?: string } | undefined;
  if (c?.type === "screen.frame.dropped") log(`frame dropped: ${c.reason ?? "?"}`);
  if (c?.type === "screen.reconnect") {
    const r = c as { attempt?: number; delay_ms?: number };
    log(`screen reconnect #${r.attempt} in ${r.delay_ms}ms (capture stays live)`);
  }
});

async function shareFrames(source: "screen" | "camera"): Promise<void> {
  // Intentional source switch (or re-selection): CaptureManager stops the
  // previous capture session before this acquires — never two streams or
  // two samplers alive at once.
  await capture.switchTo(async () => {
    const stream =
      source === "camera"
        ? await navigator.mediaDevices.getUserMedia({ video: true })
        : await navigator.mediaDevices.getDisplayMedia({ video: true });
    const track = stream.getVideoTracks()[0];
    gnsis.sendHostEvent({
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
    track.onended = () => capture.stopIfCurrent(sessionHandle);

    const canvas = document.createElement("canvas");
    const c2d = canvas.getContext("2d")!;
    const rateHz = resolveFrameRate(screenChannel?.recommended_frame_rate, FRAME_FALLBACK_HZ);
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
        const blob = await new Promise<Blob | null>((r) =>
          canvas.toBlob(r, "image/jpeg", FRAME_JPEG_QUALITY),
        );
        // Stopped mid-flight: drop the frame rather than pushing a stale one.
        if (!blob || stopped) return;
        const bytes = new Uint8Array(await blob.arrayBuffer());
        gnsis.sendScreenFrame(
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
    log(
      `${source} on: ${video.videoWidth || "?"}x${video.videoHeight || "?"} ` +
        `→ ≤${FRAME_FIT_PX}px @ ${rateHz}Hz`,
    );
    statsTimer = setInterval(() => {
      if (framesSent === 0) return;
      if (statsTimer) clearInterval(statsTimer);
      statsTimer = null;
      log(
        `${source} sampling: ${canvas.width}x${canvas.height} · ` +
          `${(lastBytes / 1024).toFixed(1)}KB/frame · ${rateHz}Hz`,
      );
    }, 2000);
    return sessionHandle;
  });
}

// ---- wire up ----------------------------------------------------------------

gnsis.onControl((c: any) => {
  if (c?.type === "playback.ack.done" || c?.type === "host.event.done") return;
  log(`<- ${c?.type ?? "?"} ${JSON.stringify(c).slice(0, 160)}`);
  if (c?.type === "playback.cancel") interruptPlayback("daemon_cancel");
  if (c?.type === "ready") setStatus(`session ${c.session_id}`);
});
gnsis.onAudio((pcm) => void playPcm(pcm));
gnsis.onClosed((code) => setStatus(`closed ${code}`));
gnsis.onInterrupted(() => interruptPlayback("global_shortcut"));

void gnsis.mediaPermissions().then((p) => {
  for (const [k, v] of Object.entries(p ?? {})) {
    if (v === "granted" || v === "denied") {
      gnsis.sendHostEvent({ type: "permission.changed", permission: k, state: v, ts_ms: Date.now() });
    }
  }
});

document.getElementById("mic")!.onclick = () => (micStream ? stopMic() : startMic());
document.getElementById("screen")!.onclick = () => void shareFrames("screen");
document.getElementById("camera")!.onclick = () => void shareFrames("camera");
document.getElementById("interrupt")!.onclick = () => interruptPlayback();
document.getElementById("search")!.onclick = async () => {
  const q = (document.getElementById("query") as HTMLInputElement).value;
  const res = await gnsis.callTool("internet_search", { query: q });
  log(`tool internet_search -> ${JSON.stringify(res).slice(0, 300)}`);
};
