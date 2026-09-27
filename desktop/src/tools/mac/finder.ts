/**
 * What Finder can tell the files tool, over Apple Events.
 *
 * The first use asks the person, through macOS, to let GNSIS control Finder.
 * If they said no, the selection is reported as needing that permission —
 * never silently treated as "nothing selected".
 */
import { ActionProblem } from "../actions.js";
import type { FinderBridge } from "../files.js";
import { jxa, type ScriptFailure, type Shell } from "./shell.js";

export class MacFinder implements FinderBridge {
  constructor(private readonly shell: Shell) {}

  async selection(): Promise<string[]> {
    const out = await this.script(
      `JSON.stringify(Application("Finder").selection().map(i => decodeURI(i.url()).replace(/^file:\\/\\//, "")))`,
    );
    return parseList(out).map(stripSlash);
  }

  async frontFolder(): Promise<string | null> {
    try {
      const out = await this.script(
        `const f = Application("Finder"); f.finderWindows.length ? decodeURI(f.finderWindows[0].target().url()).replace(/^file:\\/\\//, "") : ""`,
      );
      return out ? stripSlash(out) : null;
    } catch (err) {
      // No window is an answer; a refusal still surfaces through selection().
      if (err instanceof ActionProblem && err.status === "needs_permission") return null;
      return null;
    }
  }

  async search(folder: string, query: string): Promise<string[] | null> {
    const result = await this.shell.run("/usr/bin/mdfind", ["-onlyin", folder, "-name", query], { timeoutMs: 8_000 });
    if (result.code !== 0) return null;
    return result.stdout.split("\n").filter(Boolean).slice(0, 500);
  }

  private async script(source: string): Promise<string> {
    try {
      return await jxa(this.shell, source, "Finder");
    } catch (err) {
      throw scriptProblem(err, "Finder");
    }
  }
}

/** A classified script failure, as the model and the person should hear it. */
export function scriptProblem(err: unknown, app: string): ActionProblem {
  if (err instanceof ActionProblem) return err;
  const failure = err as ScriptFailure;
  switch (failure?.kind) {
    case "automation_denied":
      return new ActionProblem(
        "needs_permission",
        `macOS has not let GNSIS control ${app}. Turn on ${app} under GNSIS in System Settings → Privacy & Security → Automation, then ask again.`,
        { permission: "automation", app },
      );
    case "accessibility_denied":
      return new ActionProblem(
        "needs_permission",
        "macOS has not let GNSIS control the computer. Turn on GNSIS in System Settings → Privacy & Security → Accessibility, then ask again.",
        { permission: "accessibility" },
      );
    case "not_running":
      return new ActionProblem("not_found", `${app} is not open.`, { app });
    case "no_window":
      return new ActionProblem("not_found", `${app} has no window open.`, { app });
    default:
      return new ActionProblem("failed", `${app} did not do it${failure?.message ? `: ${failure.message}` : "."}`, { app });
  }
}

function parseList(out: string): string[] {
  try {
    const value = JSON.parse(out) as unknown;
    return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
  } catch {
    return [];
  }
}

function stripSlash(p: string): string {
  return p.length > 1 && p.endsWith("/") ? p.slice(0, -1) : p;
}
