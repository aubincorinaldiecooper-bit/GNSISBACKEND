/**
 * GNSIS desktop renderer — owns the actual devices.
 *
 * Mic: getUserMedia -> AudioWorklet resample -> 16 kHz PCM16 chunks ->
 * `audio.frame` header + binary on /ws/duplex.
 * Screen/camera: capture -> JPEG -> `screen.frame` metadata + binary on
 * /ws/screen. Playback: model PCM plays at 24 kHz; each utterance's
 * start/finish sends playback.ack — the device's word that audio happened.
 */

declare const gnsis: {
  mediaPermissions(): Promise<Record<string, unknown>>;
  sendControl(control: unknown): void;
  sendAudioFrame(header: unknown, pcm: Uint8Array): void;
  sendScreenFrame(metadata: unknown, payload: Uint8Array): void;
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
const setStatus = (m: string) => (document.getElementById("status")!.textContent = m);

// ---- playback + ACK truth -------------------------------------------------

const playbackCtx = new AudioContext({ sampleRate: 24000 });
let playbackId = 0;
let currentEpoch = 0;
let currentSource: AudioBufferSourceNode | null = null;

async function playPcm(pcm: Uint8Array): Promise<void> {
  const int16 = new Int16Array(pcm.buffer, pcm.byteOffset, pcm.byteLength / 2);
  const buf = playbackCtx.createBuffer(1, int16.length, 24000);
  const chan = buf.getChannelData(0);
  for (let i = 0; i < int16.length; i++) chan[i] = int16[i] / 32768;
  const src = playbackCtx.createBufferSource();
  src.buffer = buf;
  src.connect(playbackCtx.destination);
  const id = `pb-${++playbackId}`;
  gnsis.sendControl({ type: "playback.ack", playback_id: id, phase: "started", epoch: currentEpoch });
  src.onended = () => {
    if (currentSource === src) currentSource = null;
    gnsis.sendControl({ type: "playback.ack", playback_id: id, phase: "finished", epoch: currentEpoch });
  };
  currentSource = src;
  src.start();
}

function interruptPlayback(): void {
  try {
    currentSource?.stop();
  } catch {
    /* already ended */
  }
  gnsis.sendControl({ type: "break", reason: "user_interrupt" });
}

// ---- mic -------------------------------------------------------------------

let seq = 0;
let startSample = 0;
let micStream: MediaStream | null = null;

async function startMic(): Promise<void> {
  micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
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
  log("mic off");
}

// ---- screen / camera ---------------------------------------------------------

async function shareFrames(source: "screen" | "camera"): Promise<void> {
  let stream: MediaStream;
  if (source === "camera") {
    stream = await navigator.mediaDevices.getUserMedia({ video: true });
  } else {
    stream = await navigator.mediaDevices.getDisplayMedia({ video: true });
  }
  const track = stream.getVideoTracks()[0];
  const canvas = document.createElement("canvas");
  const c2d = canvas.getContext("2d")!;
  const grab = async () => {
    if (!micStream && false) return;
    // Reuse getDisplayMedia's track via ImageCapture where available.
    const capture = new (ImageCapture as unknown as { new (t: MediaStreamTrack): { grabFrame(): Promise<ImageBitmap> } })(track);
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
  if (c?.type === "playback.ack.done") return;
  log(`<- ${c?.type ?? "?"} ${JSON.stringify(c).slice(0, 160)}`);
  if (c?.type === "ready") setStatus(`session ${c.session_id}`);
});
gnsis.onAudio((pcm) => void playPcm(pcm));
gnsis.onClosed((code) => setStatus(`closed ${code}`));
gnsis.onInterrupted(interruptPlayback);

document.getElementById("mic")!.onclick = () =>
  micStream ? stopMic() : startMic();
document.getElementById("screen")!.onclick = () => void shareFrames("screen");
document.getElementById("camera")!.onclick = () => void shareFrames("camera");
document.getElementById("interrupt")!.onclick = interruptPlayback;
document.getElementById("search")!.onclick = async () => {
  const q = (document.getElementById("query") as HTMLInputElement).value;
  const res = await gnsis.callTool("internet_search", { query: q });
  log(`tool internet_search -> ${JSON.stringify(res).slice(0, 300)}`);
};
