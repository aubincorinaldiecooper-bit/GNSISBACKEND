import assert from "node:assert/strict";
import { test } from "node:test";
import { base32, deriveId, shareLink } from "./identity";

test("the public ID is the base32 of the key's SHA-256, twelve characters grouped 4-4-4", async () => {
  // SHA-256 of nothing is e3b0c442…; its RFC 4648 base32 begins 4OYMIQUY7QOB…
  assert.equal(await deriveId(new Uint8Array()), "gnsis:4OYM-IQUY-7QOB");
  const key = new Uint8Array([4, 1, 2, 3]);
  const once = await deriveId(key);
  assert.equal(once, await deriveId(key), "the same key always gives the same ID");
  assert.match(once, /^gnsis:[A-Z2-7]{4}-[A-Z2-7]{4}-[A-Z2-7]{4}$/);
});

test("base32 uses the RFC alphabet and stops at the requested length", () => {
  assert.equal(base32(new Uint8Array([0, 0]), 3), "AAA");
  assert.equal(base32(new Uint8Array([255, 255, 255, 255, 255]), 8), "77777777");
  assert.equal(base32(new Uint8Array([255, 255, 255, 255, 255]), 3), "777");
});

test("the share link drops the gnsis: prefix", () => {
  assert.equal(shareLink({ publicId: "gnsis:7FK3-C918-4E2A", publicKey: "", storage: "local" }), "https://gnsis.studio/7FK3-C918-4E2A");
});
