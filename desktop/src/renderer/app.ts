/**
 * GNSIS desktop renderer — owns the actual devices (Electron implementation
 * of the capture/playback adapters). It reports only neutral HostEvents
 * upstream: playback.started/completed/cancelled, device.changed,
 * permission.changed. The daemon never sees a DOM or Electron API.
 */

import type { ScreenFrameMetadata } from "../shared/protocol.js";

declare const gnsis: {
  mediaPermissions(): Promise<Record<string, unknown>>;
  requestPermission(kind: string): Promise<string>;
  captureScreenshot(): Promise<Uint8Array>;
  sendControl(control: unknown): void;
  sendHostEvent(event: unknown): void;
  startCall(): void;
  sendAudioFrame(header: unknown, pcm: Uint8Array): void;
  sendScreenFrame(metadata: ScreenFrameMetadata, payload: Uint8Array): void;
  callTool(tool: string, args: Record<string, unknown>): Promise<unknown>;
  onControl(fn: (control: unknown) => void): void;
  onAudio(fn: (pcm: Uint8Array) => void): void;
  onClosed(fn: (code: number, reason: string) => void): void;
  onInterrupted(fn: () => void): void;
};

const log = (m: string) => {
  const el = document.getElementById("log")!;
  el.textContent += `${new Date().toISOString().slice(11, 19)} ${m}\n`;
  el.scrollTop = el.scrollHeight;
};
const setStatus = (m: string) =>
  (document.getElementById("status")!.textContent = m);

// ---- playback + ACK truth ---------------------------------------------------

const playbackCtx = new AudioContext({ sampleRate: 24000 });
let playbackSeq = 0;
let outputEpoch = 0;
let currentSource: AudioBufferSourceNode | null = null;

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
    if (currentSource === src) currentSource = null;
    gnsis.sendHostEvent({ type: "playback.completed", playback_id, output_epoch: outputEpoch, ts_ms: Date.now() });
  };
  currentSource = src;
  src.start();
}

function interruptPlayback(reason = "user_interrupt"): void {
  try {
    currentSource?.stop();
  } catch {
    /* already ended */
  }
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
  micStream?.getTracks().forEach((t) => t.stop());
  micStream = null;
  gnsis.sendControl({ type: "stop" });
  log("mic off");
}

// ---- screen / camera ---------------------------------------------------------

async function shareFrames(source: "screen" | "camera"): Promise<void> {
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
  const canvas = document.createElement("canvas");
  const c2d = canvas.getContext("2d")!;
  const grab = async () => {
    const capture = new (
      ImageCapture as unknown as {
        new (t: MediaStreamTrack): { grabFrame(): Promise<ImageBitmap> };
      }
    )(track);
    const bmp = await capture.grabFrame();
    canvas.width = bmp.width;
    canvas.height = bmp.height;
    c2d.drawImage(bmp, 0, 0);
    const blob = await new Promise<Blob | null>((r) => canvas.toBlob(r, "image/jpeg", 0.7));
    if (!blob) return;
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
      new Uint8Array(await blob.arrayBuffer()),
    );
  };
  const timer = setInterval(grab, 1000);
  track.onended = () => clearInterval(timer);
  log(`${source} on`);
}

// ---- wire up ----------------------------------------------------------------

gnsis.onControl((c: any) => {
  if (c?.type === "playback.ack.done" || c?.type === "host.event.done") return;
  log(`<- ${c?.type ?? "?"} ${JSON.stringify(c).slice(0, 160)}`);
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
