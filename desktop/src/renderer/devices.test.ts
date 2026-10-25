import assert from "node:assert/strict";
import { test } from "node:test";
import { Mic, Vision } from "./devices.js";
import type { GnsisBridge } from "./bridge.js";

type Track = { label: string; onended: null | (() => void); stopped: boolean; stop(): void };
function fakeStream(label: string) {
  const track: Track = { label, onended: null, stopped: false, stop() { this.stopped = true; } };
  return { id: label, getVideoTracks: () => [track], getTracks: () => [track], track };
}

test("a screen picked late, after the person switched to the camera, never replaces the camera's picture", async () => {
  const saved = { navigator: globalThis.navigator, document: (globalThis as { document?: unknown }).document };
  let answerScreen!: (s: unknown) => void;
  Object.defineProperty(globalThis, "navigator", {
    configurable: true,
    value: {
      mediaDevices: {
        getDisplayMedia: () => new Promise((r) => (answerScreen = r)),
        getUserMedia: async () => fakeStream("camera"),
      },
    },
  });
  (globalThis as { document?: unknown }).document = {
    createElement: (tag: string) =>
      tag === "video"
        ? { muted: false, srcObject: null, videoWidth: 0, videoHeight: 0, play: async () => {} }
        : { width: 0, height: 0, getContext: () => ({ drawImage() {} }), toBlob() {} },
  };
  const bridge = { sendHostEvent() {}, sendScreenFrame() {} } as unknown as GnsisBridge;
  const vision = new Vision(bridge, () => {});
  const tick = () => new Promise((r) => setTimeout(r, 0));
  try {
    const screen = vision.share("screen"); // the system picker opens
    await tick();
    assert.equal(vision.stream, null, "nothing to show while the picker is open");
    vision.stop(); // Cancel in the card, picker still open
    await vision.share("camera");
    assert.equal((vision.stream as unknown as { id: string } | null)?.id, "camera");
    const late = fakeStream("screen");
    answerScreen(late); // the person then picks a screen in the still-open picker
    await screen;
    await tick();
    assert.equal(vision.current, "camera");
    assert.equal((vision.stream as unknown as { id: string } | null)?.id, "camera", "the camera's picture is still the one shown");
    assert.equal(late.track.stopped, true, "the late screen capture was released");
    vision.stop();
    assert.equal(vision.stream, null, "stopped: nothing to show");
  } finally {
    vision.stop();
    Object.defineProperty(globalThis, "navigator", { configurable: true, value: saved.navigator });
    (globalThis as { document?: unknown }).document = saved.document;
  }
});

// ---- microphone frames across calls -----------------------------------------
// A call ends with `stop: false`, so the next call arrives on the same daemon
// session. The daemon's audio timeline refuses frames that do not continue
// its own count, and it was left holding `1` after the previous call, so every
// call has to reopen at sequence 1 / start_sample 0. This is the regression
// guard for "audio frame sequence must be 1, got 1079".

type AudioHarness = {
  mic: Mic;
  frames: Array<{ sequence: number; start_sample: number; sample_count: number }>;
  /** Post one 320-sample worklet chunk, as the real worklet would. */
  emit(): void;
  restored(): void;
};

function audioHarness(): AudioHarness {
  const saved = {
    navigator: globalThis.navigator,
    AudioContext: (globalThis as { AudioContext?: unknown }).AudioContext,
    AudioWorkletNode: (globalThis as { AudioWorkletNode?: unknown }).AudioWorkletNode,
  };
  const frames: AudioHarness["frames"] = [];
  let emit: (() => void) | null = null;
  let capture = 0;
  Object.defineProperty(globalThis, "navigator", {
    configurable: true,
    value: {
      mediaDevices: {
        getUserMedia: async () => {
          capture += 1;
          const track = { label: `input-${capture}`, stop() {} };
          return { getAudioTracks: () => [track], getTracks: () => [track] };
        },
      },
    },
  });
  const g = globalThis as Record<string, unknown>;
  // A capture context whose graph is inert; only the worklet's messages matter.
  g.AudioContext = class {
    currentTime = 0;
    destination = {};
    audioWorklet = { addModule: async () => {} };
    createMediaStreamSource() { return { connect() {} }; }
    close() { return Promise.resolve(); }
  };
  g.AudioWorkletNode = class {
    port: { onmessage: ((e: { data: ArrayBuffer }) => void) | null } = { onmessage: null };
    connect() {}
    disconnect() {}
    constructor() {
      emit = () => this.port.onmessage?.({ data: new Int16Array(320).buffer });
    }
  };
  const bridge = {
    sendAudioFrame(header: unknown) {
      frames.push(header as AudioHarness["frames"][number]);
    },
    sendHostEvent() {},
  } as unknown as GnsisBridge;
  return {
    mic: new Mic(bridge, () => {}),
    frames,
    emit: () => emit?.(),
    restored() {
      Object.defineProperty(globalThis, "navigator", { configurable: true, value: saved.navigator });
      g.AudioContext = saved.AudioContext;
      g.AudioWorkletNode = saved.AudioWorkletNode;
    },
  };
}

test("a second call on the same session reopens the microphone's frame count at 1", async () => {
  const h = audioHarness();
  try {
    await h.mic.start();
    h.emit();
    h.emit();
    assert.deepEqual(
      h.frames.map((f) => [f.sequence, f.start_sample]),
      [[1, 0], [2, 320]],
      "the first call counts from 1 and keeps its sample clock",
    );
    // The call ends (live_ended, stop: false); the next call opens the mic again.
    h.mic.stop();
    await h.mic.start();
    h.emit();
    assert.deepEqual(
      [h.frames.at(-1)?.sequence, h.frames.at(-1)?.start_sample],
      [1, 0],
      "the next call must not continue the previous call's count",
    );
  } finally {
    h.restored();
  }
});

test("mute then unmute keeps counting, because it is the same call", async () => {
  const h = audioHarness();
  try {
    await h.mic.start();
    h.emit();
    h.emit();
    // Mute releases capture only; unmute reopens it inside the same call, and
    // the daemon is still expecting the very next number.
    h.mic.stop();
    await h.mic.start();
    h.emit();
    assert.deepEqual(
      h.frames.map((f) => f.sequence),
      [1, 2, 1],
      "an unmute inside a call restarts at 1, matching the daemon's per-call baseline",
    );
  } finally {
    h.restored();
  }
});
