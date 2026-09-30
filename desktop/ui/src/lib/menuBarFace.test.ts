import assert from "node:assert/strict";
import { test } from "node:test";
import { blobatar } from "blobatar";
import { GNSIS_FACE } from "./face";
import { menuBarGlyph } from "./menuBarFace";

const circles = (svg: string) => svg.match(/<circle [^>]*\/>/g) ?? [];

test("the menu bar icon is this GNSIS's own face, in one colour, with the eyes cut out", () => {
  const id = "gnsis:TEST-TEST-TEST";
  const svg = menuBarGlyph(id);
  const face = blobatar(id, { ...GNSIS_FACE, size: 100 });
  assert.deepEqual(circles(svg), circles(face), "the same outline as the face the dock shows");
  assert.ok(circles(svg).length > 0);
  assert.match(svg, /<g fill="#000" mask="url\(#eyes\)">/, "black, which macOS tints to match the menu bar");
  const colour = face.match(/<g fill="([^"]+)">/)?.[1] ?? "";
  assert.ok(colour && !svg.includes(colour), "none of the face's own colour");
  const mask = svg.match(/<mask[\s\S]*?<\/mask>/)?.[0] ?? "";
  assert.equal((mask.match(/<path /g) ?? []).length, 2, "two eyes, cut out");
  assert.match(mask, /scale\(1\.4\)/, "drawn larger, so they read at 18 points");
  const [x, y, w, h] = (svg.match(/viewBox="([^"]+)"/)?.[1] ?? "").split(" ").map(Number);
  assert.equal(w, h, "square");
  assert.ok(x > 5 && y > 5 && w < 70, "cropped to the face, not the face's 100-unit box");
  assert.notEqual(menuBarGlyph("gnsis:OTHR-OTHR-OTHR"), svg, "another GNSIS has its own face");
});
