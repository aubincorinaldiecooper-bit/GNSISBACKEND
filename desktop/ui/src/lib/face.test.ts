import assert from "node:assert/strict";
import { test } from "node:test";
import { blobatar } from "blobatar";
import { GNSIS_FACE, GNSIS_ICON_NAME } from "./face";

const draw = (name: string) => blobatar(name, { ...GNSIS_FACE, size: 100 });
const colour = (svg: string) => svg.match(/<g fill="([^"]+)">/)?.[1];

test("before a person has a GNSIS, the face is the app icon's own character", () => {
  assert.equal(draw(GNSIS_ICON_NAME), blobatar(GNSIS_ICON_NAME, { tone: 0.8, size: 100 }));
});

test("every person's GNSIS is a mint cloud of their own", () => {
  const ids = ["gnsis:TEST-TEST-TEST", "gnsis:OTHR-OTHR-OTHR", "gnsis:K7PQ-2MXD-9RWA", "gnsis:ZZ4B-8HNE-3TQL"];
  const faces = ids.map(draw);
  const mint = colour(draw(GNSIS_ICON_NAME));
  assert.ok(mint);
  for (const face of faces) assert.equal(colour(face), mint, "the icon's mint");
  assert.equal(new Set(faces).size, ids.length, "no two alike");
});
