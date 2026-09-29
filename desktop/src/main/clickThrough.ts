/**
 * The floating window covers the main display's usable area (everything but
 * the menu bar and the Dock) but is see-through, so it must not swallow
 * clicks meant for the apps underneath. The renderer reports where its cards
 * are ([data-hit] rectangles, in page pixels); this watches the pointer and
 * lets the window take mouse input only while the pointer is over one of
 * them. Everywhere else, clicks and scrolls fall through.
 *
 * No Electron import, so it can be tested with a stand-in window.
 */

export type HitRect = [left: number, top: number, width: number, height: number];

/** The most rectangles a report may carry; the interface never has this many surfaces. */
const MAX_RECTS = 64;

/**
 * Rectangles from the renderer, checked before use: anything that is not a
 * list of four finite numbers with a positive size is dropped.
 */
export function hitRectsFrom(value: unknown): HitRect[] {
  if (!Array.isArray(value)) return [];
  const out: HitRect[] = [];
  for (const r of value.slice(0, MAX_RECTS)) {
    if (!Array.isArray(r) || r.length !== 4) continue;
    if (!r.every((n) => typeof n === "number" && Number.isFinite(n))) continue;
    const [left, top, width, height] = r as number[];
    if (width <= 0 || height <= 0) continue;
    out.push([left, top, width, height]);
  }
  return out;
}

/**
 * Is a point, in window pixels, over a card? Page pixels become window pixels
 * at the page's zoom, so a page drawn at 90% has its cards at 90% of the
 * positions it reports.
 */
export function overCard(rects: HitRect[], x: number, y: number, zoom: number): boolean {
  return rects.some(([l, t, w, h]) => x >= l * zoom && x < (l + w) * zoom && y >= t * zoom && y < (t + h) * zoom);
}

export interface ThroughWindow {
  isDestroyed(): boolean;
  getBounds(): { x: number; y: number; width: number; height: number };
  setIgnoreMouseEvents(ignore: boolean, options?: { forward?: boolean }): void;
  webContents: { getZoomFactor(): number };
}

export class ClickThrough {
  private rects: HitRect[] = [];
  /** Starts out letting everything through, until the page says where its cards are. */
  private ignoring = true;
  private timer: ReturnType<typeof setInterval> | null = null;
  /** While GNSIS's own clicks are being sent, every click goes through. */
  private suspended = 0;

  constructor(
    private readonly win: ThroughWindow,
    private readonly cursor: () => { x: number; y: number },
  ) {
    win.setIgnoreMouseEvents(true, { forward: true });
  }

  setRects(value: unknown): void {
    this.rects = hitRectsFrom(value);
    this.update();
  }

  /**
   * Let every click through, cards included, until resume(): for clicks GNSIS
   * sends to the app underneath, which must not land in GNSIS's own window.
   */
  suspend(): void {
    this.suspended += 1;
    if (this.win.isDestroyed() || this.ignoring) return;
    this.ignoring = true;
    this.win.setIgnoreMouseEvents(true, { forward: true });
  }

  resume(): void {
    this.suspended = Math.max(0, this.suspended - 1);
    this.update();
  }

  /** Take or release the mouse for where the pointer is right now. */
  update(): void {
    if (this.win.isDestroyed()) return this.stop();
    if (this.suspended > 0) return;
    const p = this.cursor();
    const b = this.win.getBounds();
    const over = overCard(this.rects, p.x - b.x, p.y - b.y, this.win.webContents.getZoomFactor() || 1);
    if (over === !this.ignoring) return;
    this.ignoring = !over;
    this.win.setIgnoreMouseEvents(this.ignoring, { forward: true });
  }

  /**
   * The pointer is checked about 25 times a second, so for up to 40 ms after
   * it leaves a card a click can still land in GNSIS. GNSIS's own clicks are
   * covered by suspend().
   */
  start(everyMs = 40): void {
    this.stop();
    this.timer = setInterval(() => this.update(), everyMs);
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }

  /** For tests and the host log. */
  get takingClicks(): boolean {
    return !this.ignoring;
  }
}
