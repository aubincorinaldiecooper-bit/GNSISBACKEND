/**
 * The desktop end of an action: a `tool.call` from the runtime becomes a real
 * thing done on this machine, and exactly one `tool.response` goes back.
 *
 *   tool.call (call_id)
 *     → agreed tool? arguments as the catalog says?
 *     → prepare: work out the real target, change nothing
 *     → policy: run, or ask the person first
 *     → permissions macOS must have granted
 *     → run once, check the result
 *     → tool.response (same call_id), and each step on the session timeline
 *
 * A call id runs at most once. The runtime re-sends a call that was waiting
 * when the connection dropped; a finished call is answered again from its
 * saved result, and a running one is simply left to finish.
 *
 * Renderer buttons never reach this: only the runtime's own calls do.
 */
import { checkArguments } from "../tools/catalog.js";
import { ActionProblem, type PreparedAction } from "../tools/actions.js";
import type { ToolRegistry } from "../tools/registry.js";
import { explainToPerson, judge, type PolicyVerdict, type TrustedTurn } from "./actionPolicy.js";
import type { ActionEvent } from "./protocol.js";
import type { Look } from "./screenWatch.js";

/** What the model is told about checking the result by looking. */
const LOOK_NOTE: Record<Look, string> = {
  fresh: "A view of the screen taken after this is with you now.",
  not_yet: "No new view of the screen has arrived yet; look again in a moment.",
  not_shared: "The screen is not being shared, so this cannot be checked by looking.",
};

/** The runtime keeps a model tool result under 256 tokens; stay well inside. */
const MAX_RESPONSE_CHARS = 900;
const PROGRESS_EVERY_MS = 20_000;
const MAX_REMEMBERED_CALLS = 128;

export interface ActionUpdate {
  callId: string;
  state: "working" | "waiting" | "done" | "failed" | "declined" | "needs_permission";
  text: string;
}

export interface ConfirmRequest {
  callId: string;
  summary: string;
  /** For the log. */
  reason: string;
  /** For the person: why they are being asked. */
  why: string;
  /** The action types keys or clicks into whatever app is in front. */
  typesIntoFrontApp: boolean;
}

export interface ActionBrokerDeps {
  registry: ToolRegistry;
  /** Send a control on the session's duplex socket. */
  send(control: Record<string, unknown>): void;
  /** Record a lifecycle step on the session timeline (host.event). */
  event(event: ActionEvent): void;
  /** Ask the person; resolves false if they decline or the ask is withdrawn. */
  confirm(request: ConfirmRequest, signal: AbortSignal): Promise<boolean>;
  /** Accessibility trust; `prompt` asks macOS to show its own prompt. */
  accessibility(prompt: boolean): boolean;
  /** The person's latest trusted turn, if any. */
  latestTurn(): TrustedTurn | null;
  /** Resolves once the person has stopped talking and their words are in (bounded). */
  waitForWords?(): Promise<void>;
  log(area: string, line: string): void;
  notify?(update: ActionUpdate): void;
  /** Wait until the runtime has a view of the screen taken after `sinceMs`. */
  lookAfter?(sinceMs: number): Promise<Look>;
  now?(): number;
}

interface CallRecord {
  done: boolean;
  response?: Record<string, unknown>;
  abort: AbortController;
}

export class ActionBroker {
  private accepted = new Set<string>();
  private readonly calls = new Map<string, CallRecord>();

  constructor(private readonly deps: ActionBrokerDeps) {}

  /** The runtime's `ready` says which of the offered tools it agreed to. */
  noteReady(control: Record<string, unknown>): void {
    const agreed = (control.host_tools as { accepted?: unknown } | undefined)?.accepted;
    this.accepted = new Set(Array.isArray(agreed) ? agreed.filter((n): n is string => typeof n === "string") : []);
    this.deps.log("execution", `tools agreed with runtime: ${[...this.accepted].join(",") || "none"}`);
  }

  /** Controls from the runtime that concern actions. Returns true if handled. */
  handleControl(control: Record<string, unknown>): boolean {
    switch (control.type) {
      case "ready":
        this.noteReady(control);
        return false; // everyone else wants `ready` too
      case "tool.call":
        if (typeof control.call_id === "string" && control.dispatch === "client") {
          void this.dispatch(control);
          return true;
        }
        return false;
      case "tool.timeout": {
        const record = typeof control.call_id === "string" ? this.calls.get(control.call_id) : undefined;
        // The model has been told it timed out; stop waiting on the person.
        record?.abort.abort();
        this.deps.log("execution", `call ${String(control.call_id)} timed out at the runtime`);
        return true;
      }
      case "tool.response.stale":
        this.deps.log("execution", `call ${String(control.call_id)} answer refused as stale (${String(control.reason)})`);
        return true;
      case "tool.response.queued":
        this.deps.log("execution", `call ${String(control.call_id)} answer queued for the model`);
        return true;
      default:
        return false;
    }
  }

  private async dispatch(control: Record<string, unknown>): Promise<void> {
    const callId = control.call_id as string;
    const known = this.calls.get(callId);
    if (known) {
      if (known.done && known.response) {
        this.deps.log("execution", `call ${callId} delivered again: answering from its result, not running it twice`);
        this.deps.send({ type: "tool.response", call_id: callId, content: known.response });
      }
      return;
    }
    const record: CallRecord = { done: false, abort: new AbortController() };
    this.remember(callId, record);
    const calls = Array.isArray(control.tool_calls) ? control.tool_calls : [];
    const call = calls[0] as { name?: unknown; arguments?: unknown } | undefined;
    const tool = typeof call?.name === "string" ? call.name : "";
    const args = (call?.arguments && typeof call.arguments === "object" ? call.arguments : {}) as Record<string, unknown>;
    const started = this.now();
    const base = { call_id: callId, tool };
    this.deps.event({ type: "action.requested", ...base, ts_ms: started, redelivered: control.redelivered === true, turn_id: typeof control.turn_id === "string" ? control.turn_id : null });

    let response: Record<string, unknown>;
    try {
      response = await this.carryOut(callId, tool, args, record.abort.signal, base);
    } catch (err) {
      const problem = err instanceof ActionProblem ? err : new ActionProblem("failed", `Something went wrong: ${String((err as Error)?.message ?? err)}`);
      response = { status: problem.status, message: problem.message, ...problem.detail };
      this.deps.event({ type: "action.failed", ...base, ts_ms: this.now(), status: problem.status, category: categoryOf(problem), latency_ms: this.now() - started });
      this.deps.notify?.({ callId, state: problem.status === "needs_permission" ? "needs_permission" : "failed", text: problem.message });
    }
    record.done = true;
    record.response = fit(response);
    this.deps.send({ type: "tool.response", call_id: callId, content: record.response });
    this.deps.log("execution", `call ${callId} ${tool} → ${String(record.response.status)} (${this.now() - started} ms)`);
  }

  private async carryOut(
    callId: string,
    tool: string,
    args: Record<string, unknown>,
    signal: AbortSignal,
    base: { call_id: string; tool: string },
  ): Promise<Record<string, unknown>> {
    if (!this.accepted.has(tool)) {
      throw new ActionProblem("unsupported", `${tool} was not agreed for this session, so this computer will not run it.`);
    }
    const argumentProblem = checkArguments(tool, args);
    if (argumentProblem) throw new ActionProblem("failed", `The request did not match the ${tool} tool: ${argumentProblem}.`);

    const prepared = await this.deps.registry.prepareAction(tool, args);
    // The model often answers before the person's last words are transcribed;
    // judge the action with those words, not without them.
    await this.deps.waitForWords?.();
    const verdict = judge(prepared, this.deps.latestTurn(), this.now());
    this.deps.event({
      type: "action.policy",
      ...base,
      ts_ms: this.now(),
      action: prepared.action,
      effect: prepared.effect,
      provenance: verdict.provenance,
      decision: verdict.decision,
      reason: verdict.reason,
      turn_id: verdict.turnId,
    });

    this.checkPermissions(prepared);

    if (verdict.decision === "confirm") {
      const allowed = await this.ask(callId, prepared, verdict, signal, base);
      if (!allowed) {
        this.deps.notify?.({ callId, state: "declined", text: `Not done: ${prepared.summary}` });
        return {
          status: "declined",
          message: signal.aborted
            ? "The person did not answer in time, so it was not done."
            : "The person chose not to allow it, so it was not done.",
        };
      }
    }

    const started = this.now();
    this.deps.event({ type: "action.started", ...base, ts_ms: started, action: prepared.action, effect: prepared.effect });
    this.deps.notify?.({ callId, state: "working", text: prepared.summary });
    const done = await prepared.run();
    // Anything that may have changed what is on screen is checked by looking:
    // hold the answer until a frame taken after the action has gone out.
    const look = prepared.effect === "read" ? undefined : await this.deps.lookAfter?.(this.now());
    this.deps.event({
      type: "action.completed",
      ...base,
      ts_ms: this.now(),
      action: prepared.action,
      status: "done",
      verified: done.verified,
      screen: look,
      latency_ms: this.now() - started,
    });
    this.deps.notify?.({ callId, state: "done", text: done.message });
    return {
      status: "done",
      message: done.message,
      verified: done.verified,
      ...(look ? { screen: LOOK_NOTE[look] } : {}),
      ...(done.detail ?? {}),
    };
  }

  private checkPermissions(prepared: PreparedAction): void {
    for (const need of prepared.needs ?? []) {
      if (need.kind === "accessibility" && !this.deps.accessibility(false)) {
        // Shows macOS's own prompt with the button into System Settings.
        this.deps.accessibility(true);
        throw new ActionProblem(
          "needs_permission",
          "macOS has not let GNSIS control the computer yet. It has just shown where to turn it on: System Settings → Privacy & Security → Accessibility, then GNSIS. Ask again once it is on.",
          { permission: "accessibility" },
        );
      }
    }
  }

  private async ask(
    callId: string,
    prepared: PreparedAction,
    verdict: PolicyVerdict,
    signal: AbortSignal,
    base: { call_id: string; tool: string },
  ): Promise<boolean> {
    const reason = verdict.reason;
    this.deps.event({ type: "action.confirmation", ...base, ts_ms: this.now(), status: "asked", reason });
    this.deps.notify?.({ callId, state: "waiting", text: `Waiting for your OK: ${prepared.summary}` });
    // Keep the runtime from declaring the call lost while the person reads.
    const progress = () => this.deps.send({ type: "tool.progress", call_id: callId, state: "awaiting_confirmation" });
    progress();
    const timer = setInterval(progress, PROGRESS_EVERY_MS);
    try {
      const allowed = await this.deps.confirm(
        {
          callId,
          summary: prepared.summary,
          reason,
          why: explainToPerson(verdict),
          typesIntoFrontApp: prepared.effect === "input",
        },
        signal,
      );
      this.deps.event({ type: "action.confirmation", ...base, ts_ms: this.now(), status: allowed ? "allowed" : signal.aborted ? "withdrawn" : "declined" });
      return allowed && !signal.aborted;
    } finally {
      clearInterval(timer);
    }
  }

  private remember(callId: string, record: CallRecord): void {
    this.calls.set(callId, record);
    while (this.calls.size > MAX_REMEMBERED_CALLS) {
      const oldest = this.calls.keys().next().value as string;
      this.calls.delete(oldest);
    }
  }

  private now(): number {
    return this.deps.now?.() ?? Date.now();
  }
}

function categoryOf(problem: ActionProblem): string {
  if (problem.status === "needs_permission") {
    const permission = problem.detail.permission;
    return permission === "automation" ? `automation_denied:${String(problem.detail.app ?? "")}` : "accessibility_denied";
  }
  return problem.status;
}

/** Keep a result small enough for the model to take in whole. */
export function fit(response: Record<string, unknown>): Record<string, unknown> {
  let current = { ...response };
  for (let guard = 0; guard < 40 && JSON.stringify(current).length > MAX_RESPONSE_CHARS; guard += 1) {
    const longest = Object.entries(current)
      .filter(([key]) => key !== "status" && key !== "message")
      .sort((a, b) => JSON.stringify(b[1]).length - JSON.stringify(a[1]).length)[0];
    if (!longest) break;
    const [key, value] = longest;
    if (Array.isArray(value) && value.length > 1) {
      current = { ...current, [key]: value.slice(0, Math.ceil(value.length / 2)), more: true };
    } else if (typeof value === "string" && value.length > 40) {
      current = { ...current, [key]: `${value.slice(0, Math.floor(value.length / 2))}…` };
    } else {
      const { [key]: _dropped, ...rest } = current;
      current = rest;
    }
  }
  if (typeof current.message === "string" && JSON.stringify(current).length > MAX_RESPONSE_CHARS) {
    current = { status: current.status, message: (current.message as string).slice(0, MAX_RESPONSE_CHARS - 60) };
  }
  return current;
}
