/**
 * Scheduled playback state for streamed Talker output. Chunks chain
 * sequentially (each starts when the previous ends), `started` fires at the
 * actual scheduled start time rather than enqueue time, and cancelled sources
 * can never emit `completed`.
 */

export interface ScheduledSourceLike {
  start(when: number): void;
  stop(): void;
  onended: (() => void) | ((ev: Event) => void) | null;
}

export interface PlaybackChunkCallbacks {
  /** Fired when the chunk actually starts playing (scheduled start time). */
  onStarted(): void;
  /** Fired only when the chunk plays to completion — never after cancel. */
  onEnded(): void;
}

export class PlaybackScheduler {
  private sources = new Set<ScheduledSourceLike>();
  private startTimers = new Set<ReturnType<typeof setTimeout>>();
  private nextStartTime = 0;

  constructor(private readonly nowSeconds: () => number) {}

  get scheduledCount(): number {
    return this.sources.size;
  }

  /** Schedules `src` (`durationSec` long) to start at max(now, end of previous chunk). */
  schedule(src: ScheduledSourceLike, durationSec: number, cbs: PlaybackChunkCallbacks): void {
    const startAt = Math.max(this.nowSeconds(), this.nextStartTime);
    this.nextStartTime = startAt + durationSec;
    this.sources.add(src);
    src.onended = () => {
      if (this.sources.delete(src)) cbs.onEnded();
    };
    src.start(startAt);
    const delayMs = Math.max(0, (startAt - this.nowSeconds()) * 1000);
    const timer = setTimeout(() => {
      this.startTimers.delete(timer);
      if (this.sources.has(src)) cbs.onStarted();
    }, delayMs);
    this.startTimers.add(timer);
  }

  /** Stops every scheduled source and clears pending start timers. Returns the number stopped. */
  cancelAll(): number {
    for (const t of this.startTimers) clearTimeout(t);
    this.startTimers.clear();
    let stopped = 0;
    for (const src of [...this.sources]) {
      this.sources.delete(src);
      try {
        src.stop();
      } catch {
        /* already ended */
      }
      stopped++;
    }
    this.nextStartTime = 0;
    return stopped;
  }
}
