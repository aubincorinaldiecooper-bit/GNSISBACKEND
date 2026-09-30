/**
 * Things this computer remembers about GNSIS's look between launches: where
 * the person has moved it, and whether the first-launch greeting has been
 * shown. Kept in the page's own storage; if that is unavailable, GNSIS simply
 * starts where it always does and greets once per launch.
 */

/** How far the person has moved GNSIS from where it sits by default, in CSS pixels. */
export interface Place {
  dx: number;
  dy: number;
}

export interface Box {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

const PLACE_KEY = "gnsis.place";
const GREETED_KEY = "gnsis.greeted";

export const HOME: Place = { dx: 0, dy: 0 };

export function loadPlace(): Place {
  try {
    const v = JSON.parse(localStorage.getItem(PLACE_KEY) ?? "null") as Partial<Place> | null;
    if (v && Number.isFinite(v.dx) && Number.isFinite(v.dy)) return { dx: v.dx as number, dy: v.dy as number };
  } catch {
    // No storage, or something unreadable in it: start at home.
  }
  return HOME;
}

export function savePlace(p: Place): void {
  try {
    localStorage.setItem(PLACE_KEY, JSON.stringify({ dx: Math.round(p.dx), dy: Math.round(p.dy) }));
  } catch {
    // Not remembered; it still moves for now.
  }
}

/** Whether the first-launch greeting has been shown on this computer before. */
export function greetedBefore(): boolean {
  try {
    return localStorage.getItem(GREETED_KEY) === "1";
  } catch {
    return false;
  }
}

export function markGreeted(): void {
  try {
    localStorage.setItem(GREETED_KEY, "1");
  } catch {
    // Not remembered: it may greet again next launch.
  }
}

/**
 * The move closest to `p` that keeps `box` (where everything sits unmoved)
 * fully inside a `w`×`h` screen, `margin` from its edges. Where the box is too
 * big to fit that way, it stays where it sits by default on that axis.
 */
export function keepOnScreen(p: Place, box: Box, w: number, h: number, margin = 8): Place {
  const fit = (want: number, lo: number, hi: number) => (lo > hi ? 0 : Math.min(hi, Math.max(lo, want)));
  return {
    dx: fit(p.dx, margin - box.left, w - margin - box.right),
    dy: fit(p.dy, margin - box.top, h - margin - box.bottom),
  };
}
