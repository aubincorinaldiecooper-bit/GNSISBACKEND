import type { TypedTurnResult } from "./typedTurns.js";

export const MAX_LOCAL_STEPS = 12;

export interface LocalToolResult {
  status?: unknown;
  message?: unknown;
  [key: string]: unknown;
}

export interface LocalControllerContext {
  requestId: string;
  text: string;
  step: number;
  history: Array<{
    tool: string;
    args: Record<string, unknown>;
    result: LocalToolResult;
  }>;
}

export type LocalControllerDecision =
  | {
      kind: "tool";
      tool: string;
      args: Record<string, unknown>;
      confidence: number;
    }
  | { kind: "done"; message?: string }
  | { kind: "fallback"; reason?: string };

export interface LocalDecisionProvider {
  decide(context: LocalControllerContext): Promise<LocalControllerDecision>;
}

export interface LocalControllerOptions {
  provider: LocalDecisionProvider;
  tools: readonly string[];
  record(turn: { turnId: string; text: string; endedAtMs: number }): void;
  runLocal(
    callId: string,
    turnId: string,
    tool: string,
    args: Record<string, unknown>,
  ): Promise<LocalToolResult>;
  answer?(text: string): void;
  log?(area: string, line: string): void;
  now?: () => number;
  maxSteps?: number;
  minConfidence?: number;
}

/**
 * Runs a bounded local model loop through the existing ActionBroker.
 *
 * The provider may only select a tool already offered by this Host. The
 * controller itself never executes an action, and records the trusted turn
 * only when the provider has produced the first valid local tool call.
 */
export class LocalController {
  private counter = 0;
  private readonly tools: Set<string>;

  constructor(private readonly opts: LocalControllerOptions) {
    this.tools = new Set(opts.tools);
  }

  /**
   * `null` means nothing ran locally and the caller may use GNSIS 1.0.
   * Once a local action has run, the same request is never retried elsewhere.
   */
  async send(text: string): Promise<TypedTurnResult | null> {
    const now = this.opts.now?.() ?? Date.now();
    this.counter += 1;
    const requestId = `gnsis05-${now}-${this.counter}`;
    const turnId = `local-${requestId}`;
    const history: LocalControllerContext["history"] = [];
    let recorded = false;
    const maxSteps = this.opts.maxSteps ?? MAX_LOCAL_STEPS;

    for (let step = 0; step <= maxSteps; step += 1) {
      let decision: LocalControllerDecision;
      try {
        decision = await this.opts.provider.decide({
          requestId,
          text,
          step,
          history: [...history],
        });
      } catch (error) {
        this.opts.log?.(
          "local-controller",
          `decision ${requestId} failed at step ${step}: ${String(
            (error as Error)?.message ?? error,
          )}`,
        );
        if (!recorded) return null;
        return {
          ok: false,
          reason: "GNSIS 0.5 stopped after beginning this task.",
        };
      }

      if (decision.kind === "fallback") {
        if (!recorded) return null;
        return {
          ok: false,
          reason:
            decision.reason?.trim() ||
            "GNSIS 0.5 could not safely finish this task.",
        };
      }

      if (decision.kind === "done") {
        const answer = decision.message?.trim();
        if (!recorded && !answer) return null;
        if (answer) this.opts.answer?.(answer);
        return { ok: true, turnId };
      }

      // The step after the last allowed action may only finish the task.
      if (step === maxSteps) break;

      if (
        !Number.isFinite(decision.confidence) ||
        decision.confidence < (this.opts.minConfidence ?? 0.65) ||
        decision.confidence > 1
      ) {
        if (!recorded) return null;
        return {
          ok: false,
          reason: "GNSIS 0.5 was not confident enough to continue safely.",
        };
      }

      if (!this.tools.has(decision.tool)) {
        this.opts.log?.(
          "local-controller",
          `decision ${requestId} selected unavailable tool ${decision.tool}`,
        );
        return {
          ok: false,
          reason: "GNSIS 0.5 proposed an action this Mac did not offer.",
        };
      }

      if (!recorded) {
        this.opts.record({ turnId, text, endedAtMs: now });
        recorded = true;
      }
      const result = await this.opts.runLocal(
        `${requestId}-call-${step + 1}`,
        turnId,
        decision.tool,
        decision.args,
      );
      history.push({
        tool: decision.tool,
        args: decision.args,
        result: boundedResult(result),
      });
      if (result.status !== "done") {
        return {
          ok: false,
          reason:
            typeof result.message === "string"
              ? result.message
              : "The local action could not be completed.",
        };
      }
    }

    return recorded
      ? {
          ok: false,
          reason: "GNSIS 0.5 reached its step limit before finishing.",
        }
      : null;
  }
}

const SECRET_KEY = /pass(word|code|phrase)?|secret|token|cookie|api[-_]?key|private[-_]?key|credential|authori[sz]ation/i;

function boundedResult(result: LocalToolResult): LocalToolResult {
  return boundedObject(result, 0);
}

function boundedObject(value: object, depth: number): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [key, item] of Object.entries(value).slice(0, 16)) {
    if (SECRET_KEY.test(key)) continue;
    const bounded = boundedValue(item, depth);
    if (bounded !== undefined) out[key] = bounded;
  }
  return out;
}

function boundedValue(value: unknown, depth: number): unknown {
  if (typeof value === "string") return value.slice(0, 1_000);
  if (value === null || typeof value === "number" || typeof value === "boolean") return value;
  if (depth >= 3) return undefined;
  if (Array.isArray(value)) {
    return value
      .slice(0, 20)
      .map((item) => boundedValue(item, depth + 1))
      .filter((item) => item !== undefined);
  }
  if (typeof value === "object") return boundedObject(value, depth + 1);
  return undefined;
}
