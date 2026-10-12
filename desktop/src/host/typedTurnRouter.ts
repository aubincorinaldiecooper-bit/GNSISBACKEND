import type { TypedTurnResult } from "./typedTurns.js";
import { localCommand, type LocalCommand } from "./localCommands.js";
import type { LocalController } from "./localController.js";
import type { GnsisModel } from "@gnsis/ui";

export interface TypedTurnRouterOptions {
  fallback(input: unknown): Promise<TypedTurnResult>;
  controller?: Pick<LocalController, "send">;
  record(turn: { turnId: string; text: string; endedAtMs: number }): void;
  runLocal(
    callId: string,
    turnId: string,
    tool: LocalCommand["tool"],
    args: LocalCommand["args"],
  ): Promise<Record<string, unknown>>;
  now?: () => number;
}

/** Chooses the deterministic local path first, then the foreground model. */
export class TypedTurnRouter {
  private counter = 0;

  constructor(private readonly opts: TypedTurnRouterOptions) {}

  async send(
    input: unknown,
    model: GnsisModel | "auto" = "auto",
    answer?: (text: string) => void,
  ): Promise<TypedTurnResult> {
    if (model === "gnsis-1.0") return this.opts.fallback(input);
    const text = typeof input === "string" ? input.trim() : "";
    const command = text ? localCommand(text) : null;
    if (!command) {
      const local = text ? await this.opts.controller?.send(text, answer) : null;
      if (local) return local;
      if (model === "gnsis-0.5") {
        return {
          ok: false,
          reason: "GNSIS 0.5 is not available on this Mac.",
        };
      }
      return this.opts.fallback(input);
    }

    const now = this.opts.now?.() ?? Date.now();
    this.counter += 1;
    const id = `${now}-${this.counter}`;
    const turnId = `local-${id}`;
    this.opts.record({ turnId, text, endedAtMs: now });
    const response = await this.opts.runLocal(
      `local-call-${id}`,
      turnId,
      command.tool,
      command.args,
    );
    if (response.status === "done") return { ok: true, turnId };
    return {
      ok: false,
      reason:
        typeof response.message === "string"
          ? response.message
          : "The local command could not be completed.",
    };
  }
}
