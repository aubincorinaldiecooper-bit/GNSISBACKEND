import { blobatar } from "blobatar";
import { GNSIS_FACE } from "./face";

/** The eyes are drawn this much larger than in the face, so they still read at menu bar size. */
const EYES = 1.4;

type Box = { x0: number; y0: number; x1: number; y1: number };

function boxOf(nums: number[], box: Box = { x0: Infinity, y0: Infinity, x1: -Infinity, y1: -Infinity }): Box {
  for (let i = 0; i + 1 < nums.length; i += 2) {
    box = { x0: Math.min(box.x0, nums[i]), y0: Math.min(box.y0, nums[i + 1]), x1: Math.max(box.x1, nums[i]), y1: Math.max(box.y1, nums[i + 1]) };
  }
  return box;
}

const numbers = (d: string) => (d.match(/-?\d+(?:\.\d+)?/g) ?? []).map(Number);
const r2 = (n: number) => Math.round(n * 100) / 100;

/**
 * GNSIS's own face in one colour, for its icon in the Mac menu bar: the
 * outline filled black, the eyes cut out, cropped to the face. The same face
 * the dock shows (the same name and look), drawn the way macOS wants a menu
 * bar icon: black on transparent, which the menu bar then tints to match.
 */
export function menuBarGlyph(name: string): string {
  const svg = blobatar(name, { ...GNSIS_FACE, size: 100 });
  const groups = [...svg.matchAll(/<g fill="[^"]*">(.*?)<\/g>/g)].map((m) => m[1]);
  const [head = "", eyes = ""] = groups;
  let box: Box | undefined;
  for (const c of head.matchAll(/<circle cx="([\d.-]+)" cy="([\d.-]+)" r="([\d.-]+)"/g)) {
    const [cx, cy, r] = [Number(c[1]), Number(c[2]), Number(c[3])];
    box = boxOf([cx - r, cy - r, cx + r, cy + r], box);
  }
  for (const p of head.matchAll(/ d="([^"]*)"/g)) box = boxOf(numbers(p[1]), box);
  box ??= { x0: 0, y0: 0, x1: 100, y1: 100 };
  const side = Math.max(box.x1 - box.x0, box.y1 - box.y0) + 2;
  const x = r2((box.x0 + box.x1) / 2 - side / 2);
  const y = r2((box.y0 + box.y1) / 2 - side / 2);
  const cutouts = [...eyes.matchAll(/ d="([^"]*)"/g)].map((p) => {
    const b = boxOf(numbers(p[1]));
    const cx = r2((b.x0 + b.x1) / 2);
    const cy = r2((b.y0 + b.y1) / 2);
    return `<path d="${p[1]}" transform="translate(${cx} ${cy}) scale(${EYES}) translate(${-cx} ${-cy})"/>`;
  });
  return (
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${x} ${y} ${r2(side)} ${r2(side)}" width="36" height="36">` +
    `<defs><mask id="eyes" maskUnits="userSpaceOnUse" x="0" y="0" width="100" height="100">` +
    `<rect width="100" height="100" fill="#fff"/><g fill="#000">${cutouts.join("")}</g></mask></defs>` +
    `<g fill="#000" mask="url(#eyes)">${head}</g></svg>`
  );
}
