/**
 * `input`: type, press keys, or click, in whatever app is in front. The last
 * resort, for what the other tools cannot do.
 *
 * Keys and typing go through System Events; a click by name presses the
 * accessibility element with that name in the front window; a click by
 * position posts a real mouse click. All of it needs the Accessibility
 * permission, which macOS grants per app and GNSIS cannot grant itself: when
 * it is missing, GNSIS asks macOS to show the prompt and says so.
 *
 * None of it can be verified from here — only by looking — so every result
 * says that.
 */
import { ActionProblem, required, str, type ActionTool, type PreparedAction } from "../actions.js";
import { scriptProblem } from "./finder.js";
import { applescript, jxa, literal, type Shell } from "./shell.js";

/** Keys that quit, close, log out, lock, or delete: always asked about. */
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
/** Button names whose press sends, buys, deletes or commits. */
const CONSEQUENTIAL_LABEL = /\b(send|buy|pay|purchase|order|delete|remove|erase|submit|confirm|publish|post|transfer|sign|accept|agree|install|trash)\b/i;

const KEY_CODES: Record<string, number> = {
  enter: 36, return: 36, tab: 48, space: 49, delete: 51, backspace: 51, escape: 53, esc: 53,
  left: 123, right: 124, down: 125, up: 126, home: 115, end: 119, pageup: 116, pagedown: 121,
  forwarddelete: 117, f1: 122, f2: 120, f3: 99, f4: 118, f5: 96, f6: 97, f7: 98, f8: 100,
  f9: 101, f10: 109, f11: 103, f12: 111,
};
const MODIFIERS: Record<string, string> = {
  cmd: "command down", command: "command down", shift: "shift down",
  option: "option down", alt: "option down", opt: "option down",
  ctrl: "control down", control: "control down",
};

export interface ScreenGeometry {
  /** The shared display's bounds, in macOS points. */
  bounds(): { x: number; y: number; width: number; height: number };
}

export class InputTool implements ActionTool {
  readonly name = "input";
  readonly platforms = ["darwin"] as const;

  constructor(
    private readonly shell: Shell,
    private readonly screen: ScreenGeometry,
  ) {}

  async prepare(args: Record<string, unknown>): Promise<PreparedAction> {
    const action = str(args, "action");
    const needs = [{ kind: "accessibility" as const }];
    switch (action) {
      case "type": {
        const text = required(args, "text", "what to type");
        return {
          tool: this.name,
          action,
          effect: "input",
          summary: `Type “${text.length > 60 ? `${text.slice(0, 57)}…` : text}” into the app in front`,
          scope: [{ value: text, source: "named" }],
          needs,
          run: async () => {
            await this.systemEvents(`tell application "System Events" to keystroke ${literal(text)}`);
            return { verified: "screen", message: "Typed it. Look at the screen to check it went where it should." };
          },
        };
      }
      case "keys": {
        const combo = normalizeCombo(required(args, "keys", "which keys"));
        const script = keystrokeScript(combo);
        return {
          tool: this.name,
          action,
          effect: "input",
          summary: `Press ${combo}`,
          scope: [{ value: combo.split("+").pop()!, source: "named" }],
          consequential: CONSEQUENTIAL_KEYS[combo] ? `${combo} ${CONSEQUENTIAL_KEYS[combo]}` : undefined,
          needs,
          run: async () => {
            await this.systemEvents(script);
            return { verified: "screen", message: `Pressed ${combo}. Look at the screen to see what it did.` };
          },
        };
      }
      case "click": {
        const label = str(args, "label");
        if (label) {
          return {
            tool: this.name,
            action,
            effect: "input",
            summary: `Click “${label}” in the app in front`,
            scope: [{ value: label, source: "named" }],
            consequential: CONSEQUENTIAL_LABEL.test(label) ? `clicking “${label}” may send, buy, delete or commit something` : undefined,
            needs,
            run: async () => {
              const pressed = await this.pressByName(label);
              if (!pressed) {
                throw new ActionProblem("not_found", `There is nothing called “${label}” to click in the front window. Say where it is on the screen instead.`);
              }
              return { verified: "screen", message: `Clicked “${label}”. Look at the screen to see what happened.` };
            },
          };
        }
        const x = Number(args.x);
        const y = Number(args.y);
        if (!Number.isFinite(x) || !Number.isFinite(y) || x < 0 || x > 1 || y < 0 || y > 1) {
          throw new ActionProblem("failed", "Say what to click by its name, or where it is (x and y from 0 to 1).");
        }
        const b = this.screen.bounds();
        const px = Math.round(b.x + x * b.width);
        const py = Math.round(b.y + y * b.height);
        return {
          tool: this.name,
          action,
          effect: "input",
          summary: `Click at ${Math.round(x * 100)}% across, ${Math.round(y * 100)}% down the screen`,
          // A position carries no name the person could have said.
          scope: [{ value: `${px},${py}`, source: "named" }],
          needs,
          run: async () => {
            await this.clickAt(px, py);
            return { verified: "screen", message: "Clicked. Look at the screen to see what happened.", detail: { x: px, y: py } };
          },
        };
      }
      default:
        throw new ActionProblem("unsupported", `input cannot ${String(action)}.`);
    }
  }

  /** Press the combination (used by other tools, e.g. Safari back). */
  async press(combo: string): Promise<void> {
    await this.systemEvents(keystrokeScript(normalizeCombo(combo)));
  }

  private async systemEvents(script: string): Promise<void> {
    try {
      await applescript(this.shell, script, "System Events");
    } catch (err) {
      throw scriptProblem(err, "System Events");
    }
  }

  private async pressByName(label: string): Promise<boolean> {
    const source = `
      const se = Application("System Events");
      const proc = se.applicationProcesses.whose({ frontmost: true })[0];
      const want = ${literal(label.toLowerCase())};
      const names = (el) => [el.name(), el.description(), el.title && el.title()].filter(Boolean).map(String);
      let hit = null;
      const started = Date.now();
      const walk = (el, depth) => {
        if (hit || depth > 8 || Date.now() - started > 4000) return;
        let kids = [];
        try { kids = el.uiElements(); } catch (e) { return; }
        for (const k of kids) {
          let ns = [];
          try { ns = names(k); } catch (e) {}
          if (ns.some((n) => n.toLowerCase() === want)) { hit = k; return; }
          walk(k, depth + 1);
          if (hit) return;
        }
      };
      walk(proc.windows[0], 0);
      if (hit) { hit.actions.byName("AXPress").perform(); "pressed" } else { "missing" }`;
    let out: string;
    try {
      out = await jxa(this.shell, source, "System Events", 10_000);
    } catch (err) {
      throw scriptProblem(err, "System Events");
    }
    return out === "pressed";
  }

  private async clickAt(x: number, y: number): Promise<void> {
    const source = `
      ObjC.import("CoreGraphics");
      const p = $.CGPointMake(${x}, ${y});
      const down = $.CGEventCreateMouseEvent(null, $.kCGEventLeftMouseDown, p, $.kCGMouseButtonLeft);
      const up = $.CGEventCreateMouseEvent(null, $.kCGEventLeftMouseUp, p, $.kCGMouseButtonLeft);
      $.CGEventPost($.kCGHIDEventTap, down);
      $.CGEventPost($.kCGHIDEventTap, up);
      "clicked"`;
    try {
      await jxa(this.shell, source, "System Events");
    } catch (err) {
      throw scriptProblem(err, "System Events");
    }
  }
}

export function normalizeCombo(keys: string): string {
  const parts = keys
    .toLowerCase()
    .replace(/⌘/g, "cmd+").replace(/⇧/g, "shift+").replace(/⌥/g, "option+").replace(/⌃/g, "ctrl+")
    .split(/[+\s-]+/)
    .filter(Boolean)
    .map((p) => (p === "command" ? "cmd" : p === "alt" || p === "opt" ? "option" : p === "control" ? "ctrl" : p === "esc" ? "escape" : p === "return" ? "enter" : p));
  if (parts.length === 0) throw new ActionProblem("failed", "Say which keys.");
  const key = parts[parts.length - 1];
  const mods = [...new Set(parts.slice(0, -1))];
  for (const mod of mods) {
    if (!MODIFIERS[mod]) throw new ActionProblem("failed", `${mod} is not a key GNSIS knows how to hold.`);
  }
  if (!(key in KEY_CODES) && [...key].length !== 1) {
    throw new ActionProblem("failed", `${key} is not a key GNSIS knows how to press.`);
  }
  const order = ["ctrl", "cmd", "option", "shift"];
  mods.sort((a, b) => order.indexOf(a) - order.indexOf(b));
  return [...mods, key].join("+");
}

function keystrokeScript(combo: string): string {
  const parts = combo.split("+");
  const key = parts.pop()!;
  const using = parts.map((m) => MODIFIERS[m]);
  const suffix = using.length ? ` using {${using.join(", ")}}` : "";
  const code = KEY_CODES[key];
  return code !== undefined
    ? `tell application "System Events" to key code ${code}${suffix}`
    : `tell application "System Events" to keystroke ${literal(key)}${suffix}`;
}
