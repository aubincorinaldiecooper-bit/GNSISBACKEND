/**
 * One-at-a-time visual capture lifecycle.
 *
 * Intentional source switches (screen ↔ camera, or re-selecting a source)
 * end the previous capture session *before* acquiring the next one — the
 * product invariant is exactly one active capture stream + one sampler at
 * any moment. Transport (websocket) state is deliberately absent here: a
 * reconnect must never stop or reacquire the capture source, and the DOM
 * specifics live behind `CaptureHandle` so the state machine is testable.
 */
export interface CaptureHandle {
  /** Release the stream, sampler, and any timers owned by this session. */
  stop(): void;
}

export class CaptureManager {
  private current: CaptureHandle | null = null;
  /** Bumped by every stop and every switch: an acquisition that no longer matches was called off. */
  private generation = 0;

  get active(): boolean {
    return this.current !== null;
  }

  /**
   * Stop the current session, then start the next one. A stop (or another
   * switch) that comes while the next one is still being acquired — the
   * person still in the system picker, or a permission prompt still open —
   * calls it off: what it acquires is released at once, never made current.
   */
  async switchTo(create: () => Promise<CaptureHandle>): Promise<CaptureHandle> {
    this.stop();
    const mine = this.generation;
    const session = await create();
    if (mine !== this.generation) {
      session.stop();
      return session;
    }
    this.current = session;
    return session;
  }

  /**
   * Stop only if `session` is still the active one. A stale handle (e.g. an
   * `onended` event arriving after a switch already replaced it) must not
   * tear down the new capture.
   */
  stopIfCurrent(session: CaptureHandle): void {
    if (this.current === session) this.stop();
  }

  stop(): void {
    this.generation += 1;
    const session = this.current;
    this.current = null;
    session?.stop();
  }
}
