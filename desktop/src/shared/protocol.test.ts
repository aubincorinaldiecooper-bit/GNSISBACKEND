import assert from "node:assert/strict";
import { test } from "node:test";
import { ToolRegistry } from "../tools/registry.js";
import type { ScreenFrameMetadata } from "./protocol.js";
import type { VideoFrameHeader } from "../host/protocol.js";

test("audio frame header shape matches the runtime contract", async () => {
  const { AUDIO_CHUNK_MS, MIC_SAMPLE_RATE } = await import("./protocol.js");
  assert.equal((MIC_SAMPLE_RATE * AUDIO_CHUNK_MS) / 1000, 320);
});

test("tool registry: unknown tool fails cleanly", async () => {
  const reg = new ToolRegistry({ runtimeUrl: "http://x" });
  const res = await reg.call("nope", {});
  assert.equal(res.ok, false);
  assert.match(res.error ?? "", /unknown tool/);
});

test("tool registry: internet_search validates args", async () => {
  const reg = new ToolRegistry({ runtimeUrl: "http://x" });
  const res = await reg.call("internet_search", {});
  assert.equal(res.ok, false);
  assert.match(res.error ?? "", /query/);
});

// The daemon's /ws/screen socket accepts exactly `screen.frame`; camera and
// screen differ only in `video_source`. `video.frame` must never be a wire
// type — the daemon rejects it in ScreenFrameHeader.from_payload().
test("screen frame uses screen.frame + video_source screen", () => {
  const meta: ScreenFrameMetadata = {
    type: "screen.frame",
    frame_id: "f-1",
    encoding: "jpeg",
    video_source: "screen",
    captured_at_ms: 1,
  };
  assert.equal(meta.type, "screen.frame");
  assert.equal(meta.video_source, "screen");
});

test("camera frame uses screen.frame + video_source camera", () => {
  const meta: ScreenFrameMetadata = {
    type: "screen.frame",
    frame_id: "f-2",
    encoding: "jpeg",
    video_source: "camera",
    captured_at_ms: 1,
  };
  assert.equal(meta.type, "screen.frame");
  assert.equal(meta.video_source, "camera");
});

test("source switching changes video_source only, not the frame type", () => {
  const base = {
    frame_id: "f-3",
    encoding: "jpeg" as const,
    captured_at_ms: 1,
    width: 10,
    height: 10,
  };
  const screen: ScreenFrameMetadata = { ...base, type: "screen.frame", video_source: "screen" };
  const camera: ScreenFrameMetadata = { ...base, type: "screen.frame", video_source: "camera" };
  assert.equal(screen.type, camera.type);
  assert.notEqual(screen.video_source, camera.video_source);
});

test("video.frame is not part of the desktop protocol", () => {
  const bad: ScreenFrameMetadata = {
    // @ts-expect-error — the daemon only accepts type: "screen.frame"
    type: "video.frame",
    frame_id: "f-4",
    encoding: "jpeg",
    video_source: "camera",
    captured_at_ms: 1,
  };
  assert.equal(bad.type, "video.frame"); // unreachable if the type is enforced
});

test("host VideoFrameHeader is the canonical ScreenFrameMetadata shape", () => {
  const meta: VideoFrameHeader = {
    type: "screen.frame",
    frame_id: "f-5",
    encoding: "png",
    video_source: "camera",
    captured_at_ms: 2,
  };
  const shared: ScreenFrameMetadata = meta;
  assert.equal(shared.video_source, "camera");
});
