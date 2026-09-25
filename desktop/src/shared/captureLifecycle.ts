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

  get active(): boolean {
    return this.current !== null;
  }

  /** Stop the current session, then start the next one. */
  async switchTo(create: () => Promise<CaptureHandle>): Promise<CaptureHandle> {
    this.stop();
    const session = await create();
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
    const session = this.current;
    this.current = null;
    session?.stop();
  }
}
