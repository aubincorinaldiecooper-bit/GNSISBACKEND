/**
 * What the person typed into GNSIS, as their own turn.
 *
 * Typing is the same kind of evidence as speaking: words the person gave
 * this app, not words the model produced or anything read off the screen. So
 * they travel the same way spoken words do — as `turn.final` on this
 * desktop's own runtime connection, sent from the main process — and, once
 * the runtime accepts the turn, they are kept in the same TurnLog the action
 * policy reads. The renderer can ask for a turn to be sent; it cannot write
 * one into the log itself.
 *
 * A turn the runtime does not confirm is reported as not delivered, never
 * assumed to have arrived.
 */
import type { TurnLog } from "./turns.js";

/** Plenty for a typed request; well under the runtime's own limit. */
export const MAX_TYPED_CHARS = 4_000;
const ACCEPT_TIMEOUT_MS = 8_000;

/**
 * `unconfirmed`: the turn went out but was never confirmed, so the runtime
 * may have it after all — the person is not told it was "not sent".
 */
export type TypedTurnResult = { ok: true; turnId: string } | { ok: false; reason: string; unconfirmed?: boolean };

export interface TypedTurnOptions {
  /** Send a control on this desktop's duplex socket. */
  send(control: Record<string, unknown>): void;
  turns: TurnLog;
  /** Is the runtime connected and ready for this session right now? */
  ready(): boolean;
  log(area: string, line: string): void;
  now?(): number;
  acceptTimeoutMs?: number;
}

interface Waiting {
  text: string;
  sentAtMs: number;
  resolve(result: TypedTurnResult): void;
  timer: ReturnType<typeof setTimeout>;
}

export class TypedTurns {
  private counter = 0;
  private readonly waiting = new Map<string, Waiting>();

  constructor(private readonly opts: TypedTurnOptions) {}

  /** Send one typed turn. Resolves when the runtime accepts it, or with the reason it did not. */
  send(input: unknown): Promise<TypedTurnResult> {
    if (typeof input !== "string") return Promise.resolve({ ok: false, reason: "There is nothing to send." });
    const text = input.trim();
    if (!text) return Promise.resolve({ ok: false, reason: "There is nothing to send." });
    if (text.length > MAX_TYPED_CHARS) {
      return Promise.resolve({ ok: false, reason: `That message is too long to send (${MAX_TYPED_CHARS} characters at most).` });
    }
    if (!this.opts.ready()) {
      this.opts.log("turns", "typed turn not sent: the runtime is not connected");
      return Promise.resolve({ ok: false, reason: "GNSIS isn’t connected right now. Try again in a moment." });
    }
    const now = this.opts.now?.() ?? Date.now();
    this.counter += 1;
    const turnId = `typed-${now}-${this.counter}`;
    return new Promise<TypedTurnResult>((resolve) => {
      const timer = setTimeout(() => {
        if (!this.waiting.delete(turnId)) return;
        this.opts.log("turns", `typed turn ${turnId} was not confirmed by the runtime in time`);
        resolve({ ok: false, reason: "GNSIS didn’t confirm it got your message. Try again in a moment.", unconfirmed: true });
      }, this.opts.acceptTimeoutMs ?? ACCEPT_TIMEOUT_MS);
      this.waiting.set(turnId, { text, sentAtMs: now, resolve, timer });
      this.opts.send({
        type: "turn.final",
        turn_id: turnId,
        text,
        timestamp_ms: now,
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
      });
      this.opts.log("turns", `typed turn ${turnId} sent (${text.length} chars)`);
    });
  }

  /** The runtime's answers about turns. Returns true if this was one of ours. */
  handleControl(control: Record<string, unknown>): boolean {
    if (control.type !== "turn.final.accepted" || typeof control.turn_id !== "string") return false;
    const turn = this.waiting.get(control.turn_id);
    if (!turn) return false;
    this.waiting.delete(control.turn_id);
    clearTimeout(turn.timer);
    this.opts.turns.record({ turnId: control.turn_id, text: turn.text, endedAtMs: turn.sentAtMs });
    this.opts.log("turns", `typed turn ${control.turn_id} accepted by the runtime (${turn.text.length} chars)`);
    turn.resolve({ ok: true, turnId: control.turn_id });
    return true;
  }

  /** The connection closed: nothing still waiting can be confirmed any more. */
  abandonAll(reason = "The connection to GNSIS closed before it confirmed your message."): void {
    for (const [turnId, turn] of this.waiting) {
      clearTimeout(turn.timer);
      this.opts.log("turns", `typed turn ${turnId} abandoned: connection closed`);
      turn.resolve({ ok: false, reason, unconfirmed: true });
    }
    this.waiting.clear();
  }
}
