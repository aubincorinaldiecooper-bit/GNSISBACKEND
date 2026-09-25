/**
 * Mic capture lifecycle with mute semantics: `stop()` releases local tracks
 * and the AudioContext only — it must never send a session-terminal control,
 * because muting then unmuting must leave the duplex session alive.
 */

export interface MicCaptureResources {
  stream: { getTracks(): Array<{ stop(): void }> };
  ctx: { close(): Promise<void> };
}

export class MicSession {
  private resources: MicCaptureResources | null = null;

  get active(): boolean {
    return this.resources !== null;
  }

  async start(acquire: () => Promise<MicCaptureResources>): Promise<void> {
    this.stop();
    this.resources = await acquire();
  }

  stop(): void {
    if (!this.resources) return;
    const { stream, ctx } = this.resources;
    this.resources = null;
    for (const track of stream.getTracks()) track.stop();
    void ctx.close();
  }
}
