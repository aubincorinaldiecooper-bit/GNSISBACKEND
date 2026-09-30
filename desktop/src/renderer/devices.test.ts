import assert from "node:assert/strict";
import { test } from "node:test";
import { Vision } from "./devices.js";
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
