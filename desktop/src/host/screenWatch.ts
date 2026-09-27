/**
 * Whether the runtime has seen the screen since something happened.
 *
 * GNSIS checks its own actions by looking, through the same screen frames it
 * always receives — there is no separate screenshot. So after an action the
 * answer to the model waits (briefly) until a frame captured after the action
 * has actually been sent, and says whether it was: a fresh view, no view
 * because the screen is not being shared, or none yet.
 */
export type Look = "fresh" | "not_shared" | "not_yet";

/** Frames older than this mean the screen is not being shared right now. */
const SHARING_WINDOW_MS = 4_000;

export class ScreenWatch {
  private lastCapturedAt = 0;
  private lastSentAt = 0;
  private waiters: Array<{ after: number; resolve: (look: Look) => void }> = [];

  constructor(private readonly now: () => number = Date.now) {}

  /** A screen frame (not camera) was just sent to the runtime. */
  noteFrame(capturedAtMs: number, source: string): void {
    if (source !== "screen") return;
    this.lastCapturedAt = Math.max(this.lastCapturedAt, capturedAtMs);
    this.lastSentAt = this.now();
    const ready = this.waiters.filter((w) => capturedAtMs >= w.after);
    this.waiters = this.waiters.filter((w) => capturedAtMs < w.after);
    for (const waiter of ready) waiter.resolve("fresh");
  }

  sharing(): boolean {
    return this.lastSentAt > 0 && this.now() - this.lastSentAt <= SHARING_WINDOW_MS;
  }

  /** Wait for a frame captured at or after `sinceMs`, up to `timeoutMs`. */
  lookAfter(sinceMs: number, timeoutMs = 1_500): Promise<Look> {
    if (!this.sharing()) return Promise.resolve("not_shared");
    if (this.lastCapturedAt >= sinceMs) return Promise.resolve("fresh");
    return new Promise((resolve) => {
      const waiter = { after: sinceMs, resolve };
      this.waiters.push(waiter);
      setTimeout(() => {
        if (!this.waiters.includes(waiter)) return;
        this.waiters = this.waiters.filter((w) => w !== waiter);
        resolve("not_yet");
      }, timeoutMs);
    });
  }
}
