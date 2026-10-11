import { ActionProblem, required, str, type ActionTool, type PreparedAction } from "../actions.js";
import { CuaToolError, listWindows, resolveWindow, windowArgs, type CuaControl } from "../cua/driver.js";

const CONSEQUENTIAL_MENU = /\b(quit|close|send|buy|pay|purchase|order|delete|remove|erase|submit|confirm|publish|post|transfer|sign|accept|agree|install|trash|empty|log ?out|restart|shut ?down|revert|discard)\b/i;

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
    const target = await resolveWindow(this.cua, { app, window: title, onScreenOnly: false }).catch((e) => { throw problem(e); });
    const scope = [...(app ? [{ value: app, source: "named" as const }] : []), ...(title ? [{ value: title, source: "named" as const }] : [])];

    if (action === "inspect") {
      const query = str(args, "query");
      return {
        tool: this.name,
        action,
        effect: "read",
        summary: `Read ${target.appName}${target.title ? ` — ${target.title}` : ""}`,
        scope,
        needs: [{ kind: "accessibility" }],
        run: async () => {
          const state = await this.call("get_window_state", {
            ...windowArgs(target),
            ...(query ? { query } : {}),
            include_accessibility_tree: true,
            include_screenshot: false,
            max_elements: 300,
            timeout_ms: 3_000,
          });
          const detail = summarizeWindowState(state.structured);
          return {
            verified: "app",
            message: `Read the current controls and contents in ${target.appName}.`,
            detail: {
              app: target.appName,
              title: target.title,
              ...(query ? { query } : {}),
              ...detail,
            },
          };
        },
      };
    }

    if (!app && !title) throw new ActionProblem("failed", "Name the app or window.");
    if (action === "menu") {
      const path = Array.isArray(args.path)
        ? args.path.filter((item): item is string => typeof item === "string" && item.trim().length > 0).map((item) => item.trim())
        : [];
      if (!path.length || path.length > 16) throw new ActionProblem("failed", "Give the menu path, like File then Save As.");
      const named = path.join(" → ");
      return {
        tool: this.name,
        action,
        effect: "input",
        summary: `Choose ${named} in ${target.appName}`,
        scope: [...scope, { value: path[path.length - 1], source: "named" }],
        consequential: CONSEQUENTIAL_MENU.test(path[path.length - 1]) ? `choosing “${path[path.length - 1]}” may close, send, delete or commit something` : undefined,
        needs: [{ kind: "accessibility" }],
        run: async () => {
          await this.call("invoke_menu", { ...windowArgs(target), path });
          return { verified: "screen", message: `Chose ${named} in ${target.appName}. Check a fresh screen view before saying what happened.` };
        },
      };
    }
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

function summarizeWindowState(value: unknown): Record<string, unknown> {
  const root = asRecord(value);
  const raw = Array.isArray(root?.elements) ? root.elements : [];
  const elements = raw.flatMap((item) => {
    const row = asRecord(item);
    if (!row) return [];
    const role = text(row.role);
    const label = text(row.label ?? row.name ?? row.title);
    const token = text(row.element_token ?? row.elementToken);
    const valueText = safeValue(row, role, label);
    if (!role && !label && !valueText) return [];
    return [{
      ...(token ? { ref: token } : {}),
      ...(role ? { role } : {}),
      ...(label ? { label } : {}),
      ...(valueText ? { value: valueText } : {}),
      ...(typeof row.enabled === "boolean" ? { enabled: row.enabled } : {}),
      ...(typeof row.selected === "boolean" ? { selected: row.selected } : {}),
      ...(Array.isArray(row.actions) ? { actions: row.actions.filter((action): action is string => typeof action === "string").slice(0, 8) } : {}),
    }];
  });
  return {
    elements,
    total_elements: finiteNumber(root?.total_element_count ?? root?.totalElementCount) ?? elements.length,
    returned_elements: finiteNumber(root?.returned_element_count ?? root?.returnedElementCount) ?? elements.length,
    ...(root?.truncated === true ? { truncated: true } : {}),
  };
}

function safeValue(row: Record<string, unknown>, role: string, label: string): string {
  const value = text(row.value);
  if (!value) return "";
  const descriptor = `${role} ${label}`.toLocaleLowerCase();
  if (
    row.secure === true ||
    row.is_secure === true ||
    /\b(password|passcode|pin|secret|token|api key|private key|credit card|card number|cvv|cvc)\b/.test(descriptor)
  ) {
    return "[private]";
  }
  return value.slice(0, 240);
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

function text(value: unknown): string {
  return typeof value === "string" ? value.trim().slice(0, 240) : "";
}

function finiteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}
