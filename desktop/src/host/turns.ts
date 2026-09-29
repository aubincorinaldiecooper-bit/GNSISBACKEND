/**
 * The person's own words, as this desktop last sent them to the runtime.
 *
 * A turn gets here only from the person: transcribed from this machine's
 * microphone, or typed into this app, and in both cases sent as `turn.final`
 * over this desktop's own connection by the main process, then recorded when
 * the runtime accepts it. Nothing the model says, and nothing on screen, can
 * add one — which is what lets the action policy treat a matching turn as the
 * person's request.
 *
 * With no transcriber and nothing typed, no turns are recorded, and every
 * action that changes something is asked about instead of assumed.
 */
import type { TrustedTurn } from "./actionPolicy.js";

export class TurnLog {
  private readonly turns: TrustedTurn[] = [];

  record(turn: TrustedTurn): void {
    this.turns.push(turn);
    if (this.turns.length > 16) this.turns.shift();
  }

  latest(): TrustedTurn | null {
    return this.turns.at(-1) ?? null;
  }
}
