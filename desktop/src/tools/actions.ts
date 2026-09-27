/**
 * Actions: the tools the model can ask this desktop to carry out.
 *
 * Each one runs in two steps. `prepare` works out exactly what would happen
 * — which file, which folder, which app — without changing anything, and
 * says what kind of effect it has. The broker then decides whether it may
 * run as asked, needs the person's OK, or must not run; only then is `run`
 * called. So a confirmation always names the real target, never the model's
 * guess at it.
 */

/** What an action does to the machine, which is what the policy judges. */
export type Effect =
  | "read" //          looks, changes nothing (list files, list tabs)
  | "open_local" //    opens or switches something already here (an app, a folder, a tab)
  | "open_remote" //   loads a web address — the one way data could leave the machine
  | "change" //        changes files or folders
  | "input"; //        types, presses keys or clicks in another app

/** How the result was checked. `screen` means only looking can tell. */
export type Verified = "disk" | "app" | "browser" | "screen" | "none";

/**
 * A thing the action is aimed at, and where the name came from: said by the
 * model (`named`, which must match the person's own words to count as their
 * request) or taken from what the person selected (`selection`).
 */
export interface ScopeTarget {
  value: string;
  source: "named" | "selection";
}

export interface PreparedAction {
  tool: string;
  action: string;
  effect: Effect;
  /** One plain sentence: what will happen, with the real names. */
  summary: string;
  scope: ScopeTarget[];
  /** Set when this action is always confirmed, whoever asked: why. */
  consequential?: string;
  /** Permissions the action needs, checked before it runs. */
  needs?: Permission[];
  run(): Promise<ActionDone>;
}

export interface ActionDone {
  verified: Verified;
  /** What happened, in a sentence the model can repeat. */
  message: string;
  detail?: Record<string, unknown>;
}

export type Permission = { kind: "accessibility" } | { kind: "automation"; app: string };

/**
 * An outcome that is not a success, with a status the model can act on.
 * Thrown from `prepare` (nothing happened) or `run` (it did not work).
 */
export type ProblemStatus =
  | "not_found" //         no such file, folder, app or tab
  | "ambiguous" //         more than one match; the candidates are listed
  | "refused" //           GNSIS will not do this at all (delete, overwrite, run a program)
  | "needs_permission" //  macOS has not allowed it yet; the person has been shown where
  | "unsupported" //       not possible on this machine or in this app
  | "failed"; //           tried, and it did not work

export class ActionProblem extends Error {
  constructor(
    readonly status: ProblemStatus,
    message: string,
    readonly detail: Record<string, unknown> = {},
  ) {
    super(message);
  }
}

export interface ActionTool {
  name: string;
  /** The platforms this tool can run on; it is not offered anywhere else. */
  platforms: readonly NodeJS.Platform[];
  prepare(args: Record<string, unknown>): Promise<PreparedAction>;
}

export function str(args: Record<string, unknown>, key: string): string | undefined {
  const value = args[key];
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

export function required(args: Record<string, unknown>, key: string, what: string): string {
  const value = str(args, key);
  if (!value) throw new ActionProblem("failed", `Say ${what}.`, { missing: key });
  return value;
}
