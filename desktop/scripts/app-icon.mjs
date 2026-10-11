// GNSIS's app icon, generated the way blobatar makes its own marks: straight
// from the library, never drawn by hand (blobatar's site builds its favicon
// from a `blobatar()` call, and @blobatar/cli renders PNGs with resvg).
//
// The owner's pick (30 September, sheet A5): the mint cloud, surprised —
// `blobatar("gnsis-h", { tone: 0.8, expression: surprised })` on blobatar's own
// squircle — placed on Apple's macOS icon grid, where the art is 824 of 1024.
// Nothing is added or redrawn: the margin is a wider viewBox around blobatar's
// untouched geometry.
//
//   npm run icon   writes build/icon.png; electron-builder makes the .icns
import { writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { blobatar } from "blobatar";
import { surprised } from "blobatar/expression";
import { Resvg } from "@resvg/resvg-js";

const SIZE = 1024;
const ART = 824;

const art = blobatar("gnsis-h", { tone: 0.8, expression: surprised, background: "squircle" });
const pad = (100 * (SIZE / ART) - 100) / 2;
const svg = art
  .replace(/ width="\d+" height="\d+"/, "")
  .replace('viewBox="0 0 100 100"', `viewBox="${-pad} ${-pad} ${100 + 2 * pad} ${100 + 2 * pad}"`);
if (!svg.includes(`viewBox="${-pad} `)) throw new Error("blobatar's output changed shape; the icon was not written");

const out = fileURLToPath(new URL("../build/icon.png", import.meta.url));
writeFileSync(out, new Resvg(svg, { fitTo: { mode: "width", value: SIZE } }).render().asPng());
console.log(`wrote ${out} (${SIZE} × ${SIZE})`);
