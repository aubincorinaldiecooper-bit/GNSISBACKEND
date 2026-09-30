/**
 * The person's own words, from this machine's microphone, as trusted turns.
 *
 * The same audio the desktop streams to the model is watched here for where
 * the person starts and stops talking. Each finished utterance is sent to the
 * runtime's speech-to-text (`/api/asr/transcribe`, the same service the
 * browser client uses), and the words go back to the runtime as `turn.final`
 * over this desktop's own connection. Only when the runtime accepts that turn
 * is it recorded in the TurnLog, where the action policy can use it.
 *
 * Nothing on screen and nothing the model says can make a turn: it starts at
 * this microphone. When the runtime has no speech-to-text, nothing is
 * recorded and actions keep asking first — that is said once in the log, not
 * papered over.
 */
import type { AudioFrameHeader } from "../shared/protocol.js";
import type { TurnLog } from "./turns.js";

const SAMPLE_RATE = 16_000;
const WINDOW_SAMPLES = 320; //        20 ms
const SPEECH_RMS = 0.012; //           about -38 dBFS: ordinary speech into a laptop mic
const START_WINDOWS = 5; //            100 ms of speech starts an utterance
const END_WINDOWS = 35; //             700 ms of quiet ends it
const PREROLL_WINDOWS = 15; //         keep 300 ms before the start
const MIN_SPEECH_WINDOWS = 15; //      shorter than 300 ms of speech is a noise
const MAX_UTTERANCE_SAMPLES = SAMPLE_RATE * 30;
const QUIET_BACKOFF_MS = 5 * 60_000; // after "no speech-to-text here", try again later

export type Transcribe = (pcm: Buffer, startMs: number) => Promise<string>;

/** The runtime said it has no speech-to-text (503/404): stop asking for a while. */
export class TranscriberUnavailable extends Error {}

export interface UtteranceOptions {
  transcribe: Transcribe;
  /** Send `turn.final` on this desktop's duplex socket. */
  send(control: Record<string, unknown>): void;
  turns: TurnLog;
  log(area: string, line: string): void;
  now?(): number;
}

interface Pending {
  text: string;
  endedAtMs: number;
}

export class UtteranceTranscriber {
  private windows: Buffer[] = []; //   pre-roll while quiet
  private speech: Buffer[] = []; //    the utterance being heard
  private loud = 0;
  private quiet = 0;
  private spokenWindows = 0;
  private startedAtMs = 0;
  private lastFrameAtMs = 0;
  private partial = Buffer.alloc(0);
  private inFlight = 0;
  private counter = 0;
  private unavailableUntil = 0;
  private readonly pending = new Map<string, Pending>();
  private readonly waiters: Array<() => void> = [];

  constructor(private readonly opts: UtteranceOptions) {}

  /** A microphone frame, exactly as it went to the runtime. */
  feed(header: AudioFrameHeader, pcm: Buffer): void {
    const now = header.captured_at_ms;
    if (this.lastFrameAtMs && now - this.lastFrameAtMs > 1_000) this.finish(); // a gap (muted): the utterance is over
    this.lastFrameAtMs = now;
    let data = this.partial.length ? Buffer.concat([this.partial, pcm]) : pcm;
    const bytes = WINDOW_SAMPLES * 2;
    let offset = 0;
    for (; offset + bytes <= data.length; offset += bytes) {
      this.window(data.subarray(offset, offset + bytes), now);
    }
    this.partial = Buffer.from(data.subarray(offset));
    data = Buffer.alloc(0);
  }

  /** Called on a timer: ends an utterance whose audio simply stopped arriving. */
  idle(nowMs: number): void {
    if (this.speech.length && this.lastFrameAtMs && nowMs - this.lastFrameAtMs > 1_000) this.finish();
  }

  /** The runtime's answers about turns. Returns true if this was one. */
  handleControl(control: Record<string, unknown>): boolean {
    if (control.type !== "turn.final.accepted" || typeof control.turn_id !== "string") return false;
    const turn = this.pending.get(control.turn_id);
    if (!turn) return true;
    this.pending.delete(control.turn_id);
    this.opts.turns.record({ turnId: control.turn_id, text: turn.text, endedAtMs: turn.endedAtMs });
    this.opts.log("turns", `turn ${control.turn_id} accepted by the runtime (${turn.text.length} chars)`);
    this.release();
    return true;
  }

  /**
   * Wait (briefly) while the person is still talking or their last words are
   * still being transcribed: an action asked for in those words should be
   * judged with them, not without.
   */
  settled(maxMs = 3_000): Promise<void> {
    if (!this.busy()) return Promise.resolve();
    return new Promise((resolve) => {
      const done = () => {
        clearTimeout(timer);
        resolve();
      };
      const timer = setTimeout(() => {
        const i = this.waiters.indexOf(done);
        if (i >= 0) this.waiters.splice(i, 1);
        resolve();
      }, maxMs);
      this.waiters.push(done);
    });
  }

  private busy(): boolean {
    return this.speech.length > 0 || this.inFlight > 0 || this.pending.size > 0;
  }

  private release(): void {
    if (this.busy()) return;
    for (const waiter of this.waiters.splice(0)) waiter();
  }

  private window(frame: Buffer, capturedAtMs: number): void {
    const loud = rms(frame) >= SPEECH_RMS;
    if (!this.speech.length) {
      this.windows.push(Buffer.from(frame));
      if (this.windows.length > PREROLL_WINDOWS) this.windows.shift();
      this.loud = loud ? this.loud + 1 : 0;
      if (this.loud >= START_WINDOWS) {
        this.speech = this.windows.splice(0);
        this.startedAtMs = capturedAtMs - this.speech.length * 20;
        this.spokenWindows = this.loud;
        this.quiet = 0;
      }
      return;
    }
    this.speech.push(Buffer.from(frame));
    if (loud) {
      this.quiet = 0;
      this.spokenWindows += 1;
    } else {
      this.quiet += 1;
    }
    if (this.quiet >= END_WINDOWS || this.speech.length * WINDOW_SAMPLES >= MAX_UTTERANCE_SAMPLES) this.finish();
  }

  private finish(): void {
    const speech = this.speech;
    const spoken = this.spokenWindows;
    this.speech = [];
    this.loud = 0;
    this.quiet = 0;
    this.spokenWindows = 0;
    if (!speech.length) return;
    if (spoken < MIN_SPEECH_WINDOWS) {
      this.release();
      return;
    }
    const now = this.opts.now?.() ?? Date.now();
    if (now < this.unavailableUntil) {
      this.release();
      return;
    }
    const pcm = Buffer.concat(speech);
    const startMs = this.startedAtMs;
    const endMs = startMs + Math.round((pcm.length / 2 / SAMPLE_RATE) * 1000);
    this.inFlight += 1;
    this.opts.log("turns", `utterance detected (${endMs - startMs} ms); transcribing`);
    void this.opts
      .transcribe(pcm, startMs)
      .then((text) => {
        const words = text.trim();
        const took = (this.opts.now?.() ?? Date.now()) - now;
        if (!words) {
          this.opts.log("turns", `speech-to-text heard no words (${took} ms)`);
          return;
        }
        this.opts.log("turns", `speech-to-text done (${words.length} chars, ${took} ms)`);
        this.counter += 1;
        const turnId = `desk-${startMs}-${this.counter}`;
        this.pending.set(turnId, { text: words, endedAtMs: endMs });
        this.opts.send({
          type: "turn.final",
          turn_id: turnId,
          text: words,
          start_ms: startMs,
          end_ms: endMs,
          timestamp_ms: now,
          timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
        });
        this.opts.log("turns", `turn ${turnId} sent (${words.length} chars)`);
        // An unanswered turn does not hold actions up for long.
        setTimeout(() => {
          if (!this.pending.delete(turnId)) return;
          this.opts.log("turns", `turn ${turnId} was not confirmed by the runtime in time`);
          this.release();
        }, 5_000);
      })
      .catch((err: unknown) => {
        if (err instanceof TranscriberUnavailable) {
          if (now >= this.unavailableUntil) {
            this.opts.log("turns", `no speech-to-text at the runtime (${err.message}); actions will ask first`);
          }
          this.unavailableUntil = now + QUIET_BACKOFF_MS;
        } else {
          this.opts.log("turns", `transcribing an utterance failed: ${String((err as Error)?.message ?? err)}`);
        }
      })
      .finally(() => {
        this.inFlight -= 1;
        this.release();
      });
  }
}

function rms(frame: Buffer): number {
  let sum = 0;
  const samples = frame.length / 2;
  for (let i = 0; i < frame.length; i += 2) {
    const v = frame.readInt16LE(i) / 32768;
    sum += v * v;
  }
  return Math.sqrt(sum / Math.max(1, samples));
}

/** The runtime's own transcriber, reached the way the browser client reaches it. */
export function runtimeTranscriber(runtimeUrl: string, fetchFn: typeof fetch = fetch): Transcribe {
  return async (pcm, startMs) => {
    const url = new URL("/api/asr/transcribe", runtimeUrl);
    url.searchParams.set("start_ms", String(Math.round(startMs)));
    let response: Response;
    try {
      response = await fetchFn(url, {
        method: "POST",
        headers: { "Content-Type": "application/octet-stream" },
        body: new Uint8Array(pcm),
        signal: AbortSignal.timeout(20_000),
      });
    } catch (err) {
      throw new Error(`speech-to-text did not answer: ${String((err as Error)?.message ?? err)}`);
    }
    if (response.status === 503 || response.status === 404 || response.status === 401 || response.status === 403) {
      throw new TranscriberUnavailable(`HTTP ${response.status}`);
    }
    if (!response.ok) throw new Error(`speech-to-text answered HTTP ${response.status}`);
    const body = (await response.json()) as { text?: unknown };
    return typeof body.text === "string" ? body.text : "";
  };
}
