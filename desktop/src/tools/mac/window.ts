import { ActionProblem, required, str, type ActionTool, type PreparedAction } from "../actions.js";
import { CuaToolError, listWindows, resolveWindow, windowArgs, type CuaControl } from "../cua/driver.js";

export class WindowTool implements ActionTool {
  readonly name = "window";
  readonly platforms = ["darwin"] as const;

  constructor(private readonly cua: CuaControl) {}

  async prepare(args: Record<string, unknown>): Promise<PreparedAction> {
    const action = str(args, "action");
    if (action === "list") {
      return {
        tool: this.name,
        action,
        effect: "read",
        summary: "List open application windows",
        scope: [],
        run: async () => {
          const windows = await listWindows(this.cua, { onScreenOnly: args.on_screen_only !== false });
          return {
            verified: "app",
            message: `Found ${windows.length} window${windows.length === 1 ? "" : "s"}.`,
            detail: { windows: windows.slice(0, 30).map((w) => ({ app: w.appName, title: w.title, window_id: w.windowId, pid: w.pid, bounds: w.bounds })) },
          };
        },
      };
    }

    const app = str(args, "app");
    const title = str(args, "title");
    if (!app && !title) throw new ActionProblem("failed", "Name the app or window.");
    const target = await resolveWindow(this.cua, { app, window: title, onScreenOnly: false }).catch((e) => { throw problem(e); });
    const scope = [...(app ? [{ value: app, source: "named" as const }] : []), ...(title ? [{ value: title, source: "named" as const }] : [])];

    if (action === "focus") {
      return {
        tool: this.name,
        action,
        effect: "open_local",
        summary: `Bring ${target.appName}${target.title ? ` — ${target.title}` : ""} to the front`,
        scope,
        run: async () => {
          await this.call("bring_to_front", windowArgs(target));
          return { verified: "screen", message: `Brought ${target.appName} to the front. Panoptic will verify it is visible.` };
        },
      };
    }

    if (action === "move_resize") {
      const x = requiredNumber(args, "x");
      const y = requiredNumber(args, "y");
      const width = requiredNumber(args, "width", 1);
      const height = requiredNumber(args, "height", 1);
      return {
        tool: this.name,
        action,
        effect: "input",
        summary: `Move/resize ${target.appName}${target.title ? ` — ${target.title}` : ""}`,
        scope,
        run: async () => {
          await this.call("set_window_frame", { ...windowArgs(target), x, y, width, height });
          return { verified: "screen", message: "Changed the window frame. Panoptic will verify the layout.", detail: { x, y, width, height } };
        },
      };
    }

    throw new ActionProblem("unsupported", `window cannot ${String(action)}.`);
  }

  private async call(tool: string, args: Record<string, unknown>) {
    try { return await this.cua.call(tool, args); } catch (e) { throw problem(e); }
  }
}

function requiredNumber(args: Record<string, unknown>, key: string, min?: number): number {
  const n = Number(args[key]);
  if (!Number.isFinite(n) || (min !== undefined && n < min)) throw new ActionProblem("failed", `${key} must be a number${min === undefined ? "" : ` >= ${min}`}.`);
  return n;
}

function problem(error: unknown): ActionProblem {
  if (error instanceof ActionProblem) return error;
  if (error instanceof CuaToolError) return new ActionProblem(error.code?.includes("not_found") ? "not_found" : "failed", error.message, { provider: "cua", code: error.code });
  return new ActionProblem("failed", String((error as Error)?.message ?? error), { provider: "cua" });
}
