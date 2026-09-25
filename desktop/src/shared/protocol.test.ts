import assert from "node:assert/strict";
import { test } from "node:test";
import { ToolRegistry } from "../tools/registry.js";

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
