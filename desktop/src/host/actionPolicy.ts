/**
 * Whether an action may run as asked, needs the person's OK, or must not run.
 *
 * GNSIS watches the screen, so text on a web page or in a document can talk
 * the model into asking for an action the person never wanted. The defence is
 * to tie each action to something the person actually said: a trusted turn —
 * their words, transcribed from their own microphone and sent over this
 * desktop's own connection — and to check that the action's targets are in
 * those words. A match alone is not trusted; it must be a match with a turn.
 *
 *   direct_user         a fresh turn, and every named target is in its words
 *   mixed               a fresh turn, but a target came from somewhere else
 *                       (usually the screen)
 *   unknown             no fresh turn to tie it to
 *
 * (`observed_untrusted` and `delegated_result` from the brief need a signal
 * that says the call was induced by content or by a worker; no such signal
 * exists on this path yet, so such calls land in `mixed` or `unknown`, which
 * are treated at least as strictly.)
 *
 * Then, by what the action does:
 *
 *   read, open_local    run: nothing changes and nothing leaves the machine
 *   open_remote         run for direct_user; otherwise ask — a web address
 *                       is the one way data on screen could be sent away
 *   change, input       run for direct_user; otherwise ask
 *   consequential       always ask (quit, trash, send, buy, …)
 */
import type { PreparedAction } from "../tools/actions.js";

export type Provenance = "direct_user" | "mixed" | "unknown";
export type Decision = "allow" | "confirm";

export interface TrustedTurn {
  turnId: string;
  text: string;
  /** When the person stopped speaking, ms since epoch. */
  endedAtMs: number;
}

export interface PolicyVerdict {
  provenance: Provenance;
  decision: Decision;
  /** Why, in words that can go in a log and, rephrased, to the person. */
  reason: string;
  turnId: string | null;
}

/** How long after the person stops speaking their words still cover an action. */
export const TURN_FRESH_MS = 90_000;

export function judge(action: PreparedAction, turn: TrustedTurn | null, nowMs: number): PolicyVerdict {
  const fresh = turn != null && nowMs - turn.endedAtMs <= TURN_FRESH_MS && nowMs >= turn.endedAtMs - 5_000;
  const provenance: Provenance = !fresh
    ? "unknown"
    : action.scope.every((target) => target.source === "selection" || saidIn(target.value, turn!.text))
      ? "direct_user"
      : "mixed";
  const turnId = fresh ? turn!.turnId : null;

  if (action.consequential) {
    return { provenance, decision: "confirm", reason: `always asked: ${action.consequential}`, turnId };
  }
  switch (action.effect) {
    case "read":
    case "open_local":
      return { provenance, decision: "allow", reason: "changes nothing and sends nothing away", turnId };
    case "open_remote":
    case "change":
    case "input":
      if (provenance === "direct_user") {
        return { provenance, decision: "allow", reason: "asked for in the person's own words", turnId };
      }
      return {
        provenance,
        decision: "confirm",
        reason:
          provenance === "mixed"
            ? "part of it is not in what the person said"
            : "there is no record of the person asking for it",
        turnId,
      };
  }
}

/**
 * Is this name in what the person said? Loose about case, punctuation, a file
 * extension and spacing ("Q3 report" matches "q3-report.pdf"), strict about
 * the words themselves.
 */
export function saidIn(name: string, words: string): boolean {
  const said = normalize(words);
  const whole = normalize(name);
  if (!whole) return false;
  if (said.includes(whole)) return true;
  const stem = normalize(name.replace(/\.[a-z0-9]{1,6}$/i, ""));
  if (stem && said.includes(stem)) return true;
  // A host like github.com is said as "github".
  const host = /^[a-z0-9-]+(\.[a-z0-9-]+)+$/i.test(name) ? normalize(name.split(".").slice(-2, -1)[0] ?? "") : "";
  return host.length >= 3 && said.includes(host);
}

function normalize(text: string): string {
  return text
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .replace(/[^a-z0-9]+/g, "");
}

/** The same reason, said to the person in the confirmation. */
export function explainToPerson(verdict: PolicyVerdict): string {
  if (verdict.reason.startsWith("always asked: ")) {
    return `This ${verdict.reason.slice("always asked: ".length)}, so GNSIS always checks first.`;
  }
  if (verdict.provenance === "mixed") {
    return "Part of this didn’t come from what you said, so GNSIS is checking with you first.";
  }
  return "GNSIS couldn’t confirm that you asked for this, so it is checking with you first.";
}
