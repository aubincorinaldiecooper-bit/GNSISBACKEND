/**
 * GNSIS input contract backed by Cua Driver.
 *
 * The public input schema remains stable. Panoptic still provides visual
 * perception and post-action verification; Cua is only the actuator and AX
 * grounding layer.
 */
import { ActionProblem, required, str, type ActionTool, type PreparedAction } from "../actions.js";
import {
  CuaToolError,
  findElementToken,
  resolveWindow,
  windowArgs,
  type CuaControl,
  type CuaWindow,
} from "../cua/driver.js";

const CONSEQUENTIAL_KEYS: Record<string, string> = {
  "cmd+q": "quits the app in front",
  "cmd+option+esc": "opens Force Quit",
  "cmd+delete": "moves the selection to the Trash",
  "cmd+backspace": "moves the selection to the Trash",
  "cmd+shift+delete": "empties the Trash",
  "cmd+shift+backspace": "empties the Trash",
  "cmd+shift+q": "logs out",
  "ctrl+cmd+q": "locks the screen",
  "cmd+option+shift+q": "logs out",
};
const SHORTCUT_NAMES: Record<string, string[]> = {
  "cmd+c": ["copy"],
  "cmd+v": ["paste"],
  "cmd+s": ["save"],
  "cmd+f": ["find"],
  "cmd+z": ["undo"],
  "cmd+t": ["new tab"],
};
const CONSEQUENTIAL_LABEL = /\b(send|buy|pay|purchase|order|delete|remove|erase|submit|confirm|publish|post|transfer|sign|accept|agree|install|trash)\b/i;

export interface ScreenGeometry {
  bounds(): { x: number; y: number; width: number; height: number };
}

export class InputTool implements ActionTool {
  readonly name = "input";
  readonly platforms = ["darwin"] as const;

  constructor(
    private readonly cua: CuaControl,
    private readonly screen: ScreenGeometry,
  ) {}

  async prepare(args: Record<string, unknown>): Promise<PreparedAction> {
    const action = str(args, "action");
    const app = str(args, "app");
    const windowName = str(args, "window");
    const target = app || windowName ? await this.window(app, windowName) : null;
    const needs = [{ kind: "accessibility" as const }];
    switch (action) {
      case "type": {
        const text = required(args, "text", "what to type");
        return {
          tool: this.name,
          action,
          effect: "input",
          summary: `Type “${text.length > 60 ? `${text.slice(0, 57)}…` : text}”${target ? ` in ${target.appName}` : " into the app in front"}`,
          scope: [{ value: text, source: "named" }, ...(app ? [{ value: app, source: "named" as const }] : [])],
          needs,
          run: async () => {
            const w = target ?? await this.window();
            await this.call("type_text", { text, ...windowArgs(w), delivery_mode: "foreground" });
            return { verified: "screen", message: "Typed it. Panoptic will verify the result from the live screen." };
          },
        };
      }
      case "keys": {
        const combo = normalizeCombo(required(args, "keys", "which keys"));
        return {
          tool: this.name,
          action,
          effect: "input",
          summary: `Press ${combo}${target ? ` in ${target.appName}` : ""}`,
          scope: [{ value: combo.split("+").pop()!, source: "named", also: SHORTCUT_NAMES[combo] }, ...(app ? [{ value: app, source: "named" as const }] : [])],
          consequential: CONSEQUENTIAL_KEYS[combo] ? `${combo} ${CONSEQUENTIAL_KEYS[combo]}` : undefined,
          needs,
          run: async () => {
            await this.press(combo, target ?? undefined);
            return { verified: "screen", message: `Pressed ${combo}. Panoptic will verify the result from the live screen.` };
          },
        };
      }
      case "click":
      case "right_click": {
        const label = str(args, "label");
        const button = action === "right_click" ? "right" : "left";
        if (label) {
          return {
            tool: this.name,
            action,
            effect: "input",
            summary: `${button === "right" ? "Right-click" : "Click"} “${label}”${target ? ` in ${target.appName}` : " in the app in front"}`,
            scope: [{ value: label, source: "named" }, ...(app ? [{ value: app, source: "named" as const }] : [])],
            consequential: CONSEQUENTIAL_LABEL.test(label) ? `clicking “${label}” may send, buy, delete or commit something` : undefined,
            needs,
            run: async () => {
              const w = target ?? await this.window();
              const state = await this.call("get_window_state", {
                ...windowArgs(w),
                query: label,
                include_accessibility_tree: true,
                include_screenshot: false,
                max_elements: 200,
              });
              const token = findElementToken(state.structured, label);
              if (!token) throw new ActionProblem("not_found", `There is nothing called “${label}” in ${w.appName}'s current accessibility state.`);
              await this.call("click", {
                ...windowArgs(w),
                element_token: token,
                button,
                delivery_mode: "background",
              });
              return { verified: "screen", message: `${button === "right" ? "Right-clicked" : "Clicked"} “${label}”. Panoptic will verify what happened.` };
            },
          };
        }
        const point = this.point(args, "x", "y");
        return {
          tool: this.name,
          action,
          effect: "input",
          summary: `${button === "right" ? "Right-click" : "Click"} at ${Math.round(point.nx * 100)}% across, ${Math.round(point.ny * 100)}% down the screen`,
          scope: [{ value: `${point.x},${point.y}`, source: "named" }],
          needs,
          run: async () => {
            await this.call("click", {
              scope: "desktop",
              x: point.x,
              y: point.y,
              button,
              delivery_mode: "foreground",
            });
            return { verified: "screen", message: `${button === "right" ? "Right-clicked" : "Clicked"}. Panoptic will verify what happened.`, detail: { x: point.x, y: point.y } };
          },
        };
      }
      case "scroll": {
        const direction = required(args, "direction", "which direction to scroll").toLowerCase();
        if (!["up", "down", "left", "right"].includes(direction)) {
          throw new ActionProblem("failed", "Scroll direction must be up, down, left, or right.");
        }
        const amount = optionalNumber(args.amount, 3, 1, 50);
        const hasPoint = Number.isFinite(Number(args.x)) && Number.isFinite(Number(args.y));
        const point = hasPoint ? this.point(args, "x", "y") : null;
        return {
          tool: this.name,
          action,
          effect: "input",
          summary: `Scroll ${direction}${target ? ` in ${target.appName}` : ""}`,
          scope: [...(app ? [{ value: app, source: "named" as const }] : [])],
          needs,
          run: async () => {
            const callArgs: Record<string, unknown> = { direction, amount, by: "line" };
            if (point) Object.assign(callArgs, { scope: "desktop", x: point.x, y: point.y });
            else {
              const w = target ?? await this.window();
              Object.assign(callArgs, windowArgs(w));
            }
            await this.call("scroll", callArgs);
            return { verified: "screen", message: `Scrolled ${direction}. Panoptic will verify the new visual state.` };
          },
        };
      }
      case "drag": {
        const from = this.point(args, "from_x", "from_y");
        const to = this.point(args, "to_x", "to_y");
        return {
          tool: this.name,
          action,
          effect: "input",
          summary: "Drag across the shared screen",
          scope: [{ value: `${from.x},${from.y}→${to.x},${to.y}`, source: "named" }],
          needs,
          run: async () => {
            await this.call("drag", {
              scope: "desktop",
              from_x: from.x,
              from_y: from.y,
              to_x: to.x,
              to_y: to.y,
              duration_ms: optionalNumber(args.duration_ms, 350, 0, 10_000),
            });
            return { verified: "screen", message: "Dragged it. Panoptic will verify the result from the live screen." };
          },
        };
      }
      default:
        throw new ActionProblem("unsupported", `input cannot ${String(action)}.`);
    }
  }

  async press(combo: string, target?: CuaWindow): Promise<void> {
    const normalized = normalizeCombo(combo);
    const w = target ?? await this.window();
    const parts = normalized.split("+");
    if (parts.length === 1) {
      await this.call("press_key", { ...windowArgs(w), key: parts[0], delivery_mode: "foreground" });
    } else {
      await this.call("hotkey", { ...windowArgs(w), keys: parts, delivery_mode: "foreground" });
    }
  }

  private async window(app?: string, window?: string): Promise<CuaWindow> {
    try {
      return await resolveWindow(this.cua, { app, window, onScreenOnly: true });
    } catch (error) {
      throw problem(error);
    }
  }

  private async call(tool: string, args: Record<string, unknown>) {
    try {
      return await this.cua.call(tool, args);
    } catch (error) {
      throw problem(error);
    }
  }

  private point(args: Record<string, unknown>, xKey: string, yKey: string) {
    const nx = Number(args[xKey]);
    const ny = Number(args[yKey]);
    if (!Number.isFinite(nx) || !Number.isFinite(ny) || nx < 0 || nx > 1 || ny < 0 || ny > 1) {
      throw new ActionProblem("failed", `${xKey} and ${yKey} must be numbers from 0 to 1.`);
    }
    const b = this.screen.bounds();
    return {
      nx,
      ny,
      x: Math.round(b.x + nx * b.width),
      y: Math.round(b.y + ny * b.height),
    };
  }
}

function optionalNumber(value: unknown, fallback: number, min: number, max: number): number {
  const n = Number(value);
  if (!Number.isFinite(n)) return fallback;
  return Math.max(min, Math.min(max, n));
}

function problem(error: unknown): ActionProblem {
  if (error instanceof ActionProblem) return error;
  if (error instanceof CuaToolError) {
    if (error.code?.includes("permission") || error.code?.includes("consent")) {
      return new ActionProblem("needs_permission", error.message, { provider: "cua", code: error.code });
    }
    if (error.code?.includes("not_found") || error.code?.includes("no_window")) {
      return new ActionProblem("not_found", error.message, { provider: "cua", code: error.code });
    }
    if (error.code?.includes("unsupported") || error.code?.includes("unavailable")) {
      return new ActionProblem("unsupported", error.message, { provider: "cua", code: error.code });
    }
    return new ActionProblem("failed", error.message, { provider: "cua", code: error.code });
  }
  return new ActionProblem("failed", String((error as Error)?.message ?? error), { provider: "cua" });
}

export function normalizeCombo(keys: string): string {
  const compact = keys
    .replace(/⌘/g, "cmd+")
    .replace(/⇧/g, "shift+")
    .replace(/⌥/g, "option+")
    .replace(/⌃/g, "ctrl+");
  const parts = compact
    .toLowerCase()
    .split(/[+\s-]+/)
    .filter(Boolean)
    .map((p) => p === "command" ? "cmd" : p === "alt" || p === "opt" ? "option" : p === "control" ? "ctrl" : p === "esc" ? "escape" : p === "return" ? "enter" : p);
  if (!parts.length) throw new ActionProblem("failed", "Say which keys.");
  const allowedModifiers = new Set(["cmd", "shift", "option", "ctrl", "fn"]);
  const key = parts[parts.length - 1];
  const mods = [...new Set(parts.slice(0, -1))];
  for (const mod of mods) if (!allowedModifiers.has(mod)) throw new ActionProblem("failed", `${mod} is not a key GNSIS knows how to hold.`);
  if (![...key].length || ([...key].length > 1 && !/^(enter|tab|space|delete|backspace|escape|left|right|up|down|home|end|pageup|pagedown|forwarddelete|f([1-9]|1[0-2]))$/.test(key))) {
    throw new ActionProblem("failed", `${key} is not a key GNSIS knows how to press.`);
  }
  const order = ["ctrl", "cmd", "option", "shift", "fn"];
  mods.sort((a, b) => order.indexOf(a) - order.indexOf(b));
  return [...mods, key].join("+");
}
