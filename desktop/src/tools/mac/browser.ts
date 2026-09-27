/**
 * `browser`: the browser the person already has open, in their own tabs and
 * signed-in sessions.
 *
 * Driven through the browser's own Apple Events scripting, which reaches the
 * running browser and its current profile without launching a copy or
 * touching cookies and passwords. macOS asks the person once per browser
 * ("GNSIS wants to control Google Chrome"). Page content is not read here —
 * GNSIS looks at the page on screen, the same way it sees everything else.
 *
 * Which browser: the one in front, else the first of the known ones running.
 */
import { ActionProblem, required, str, type ActionTool, type PreparedAction } from "../actions.js";
import { asWebAddress } from "./open.js";
import { scriptProblem } from "./finder.js";
import { jxa, literal, type Shell } from "./shell.js";

type Family = "chromium" | "safari";
const BROWSERS: Array<{ app: string; family: Family }> = [
  { app: "Google Chrome", family: "chromium" },
  { app: "Safari", family: "safari" },
  { app: "Arc", family: "chromium" },
  { app: "Brave Browser", family: "chromium" },
  { app: "Microsoft Edge", family: "chromium" },
  { app: "Chromium", family: "chromium" },
  { app: "Vivaldi", family: "chromium" },
];

export interface Tab {
  n: number;
  title: string;
  url: string;
  active: boolean;
}

export class BrowserTool implements ActionTool {
  readonly name = "browser";
  readonly platforms = ["darwin"] as const;

  constructor(
    private readonly shell: Shell,
    private readonly keys: (combo: string) => Promise<void>,
    private readonly wait: (ms: number) => Promise<void> = (ms) => new Promise((r) => setTimeout(r, ms)),
  ) {}

  async prepare(args: Record<string, unknown>): Promise<PreparedAction> {
    const action = str(args, "action");
    const browser = await this.pick();
    switch (action) {
      case "tabs":
        return {
          tool: this.name,
          action,
          effect: "read",
          summary: `List the tabs open in ${browser.app}`,
          scope: [],
          run: async () => {
            const tabs = await this.tabs(browser);
            return {
              verified: "browser",
              message: `${browser.app} has ${tabs.length} tab${tabs.length === 1 ? "" : "s"} in its front window.`,
              detail: { browser: browser.app, tabs: tabs.slice(0, 15).map((t) => `${t.n}${t.active ? "*" : ""}. ${t.title.slice(0, 60)} — ${hostOf(t.url)}`) },
            };
          },
        };
      case "switch": {
        const n = Number(args.tab);
        if (!Number.isInteger(n) || n < 1) throw new ActionProblem("failed", "Say which tab number.");
        return {
          tool: this.name,
          action,
          effect: "open_local",
          summary: `Switch ${browser.app} to tab ${n}`,
          scope: [],
          run: async () => {
            const tabs = await this.tabs(browser);
            if (n > tabs.length) throw new ActionProblem("not_found", `${browser.app} has only ${tabs.length} tabs.`);
            await this.script(browser, browser.family === "chromium"
              ? `Application(${literal(browser.app)}).windows[0].activeTabIndex = ${n}`
              : `const w = Application("Safari").windows[0]; w.currentTab = w.tabs[${n - 1}]`);
            const now = (await this.tabs(browser)).find((t) => t.active);
            if (now?.n !== n) throw new ActionProblem("failed", `${browser.app} did not switch tabs.`);
            return { verified: "browser", message: `Now on tab ${n}: ${now.title.slice(0, 80)}.`, detail: { url: hostOf(now.url) } };
          },
        };
      }
      case "go": {
        const url = asWebAddress(required(args, "url", "the address"));
        if (!url) throw new ActionProblem("failed", "That is not a web address.");
        const host = url.hostname.replace(/^www\./, "");
        return {
          tool: this.name,
          action,
          effect: "open_remote",
          summary: `Go to ${host} in ${browser.app}`,
          scope: [{ value: host, source: "named" }],
          run: async () => {
            await this.script(browser, browser.family === "chromium"
              ? `Application(${literal(browser.app)}).windows[0].activeTab.url = ${literal(url.toString())}`
              : `Application("Safari").windows[0].currentTab.url = ${literal(url.toString())}`);
            for (let i = 0; i < 20; i += 1) {
              await this.wait(250);
              const active = (await this.tabs(browser)).find((t) => t.active);
              if (active && hostOf(active.url) === host) {
                return { verified: "browser", message: `${browser.app} is on ${host}${active.title ? `: ${active.title.slice(0, 80)}` : ""}.`, detail: { url: hostOf(active.url) } };
              }
            }
            return { verified: "screen", message: `Asked ${browser.app} to go to ${host}; it has not arrived yet. Look at the screen.`, detail: { url: host } };
          },
        };
      }
      case "back":
      case "forward":
      case "reload": {
        return {
          tool: this.name,
          action,
          effect: "open_local",
          summary: `${action === "reload" ? "Reload" : action === "back" ? "Go back" : "Go forward"} in ${browser.app}`,
          scope: [],
          needs: browser.family === "safari" && action !== "reload" ? [{ kind: "accessibility" }] : [],
          run: async () => {
            if (browser.family === "chromium") {
              const call = action === "reload" ? "reload()" : action === "back" ? "goBack()" : "goForward()";
              await this.script(browser, `Application(${literal(browser.app)}).windows[0].activeTab.${call}`);
            } else if (action === "reload") {
              await this.script(browser, `const t = Application("Safari").windows[0].currentTab; t.url = t.url()`);
            } else {
              await this.script(browser, `Application("Safari").activate()`);
              await this.keys(action === "back" ? "cmd+[" : "cmd+]");
            }
            await this.wait(400);
            const active = (await this.tabs(browser)).find((t) => t.active);
            return { verified: "screen", message: `Done in ${browser.app}${active ? `; now on ${hostOf(active.url)}` : ""}. Look at the screen to be sure.`, detail: { url: active ? hostOf(active.url) : null } };
          },
        };
      }
      default:
        throw new ActionProblem("unsupported", `browser cannot ${String(action)}.`);
    }
  }

  /** The browser in front, else the first known one that is running. */
  private async pick(): Promise<{ app: string; family: Family }> {
    const front = await this.frontApp();
    const inFront = BROWSERS.find((b) => b.app === front);
    if (inFront) return inFront;
    for (const browser of BROWSERS) {
      // `is running` is answered locally and sends the browser no event,
      // so it needs no permission and never launches it.
      const running = await jxa(this.shell, `Application(${literal(browser.app)}).running()`, browser.app).catch(() => "false");
      if (running === "true") return browser;
    }
    throw new ActionProblem("not_found", "No web browser is open. Open one, or ask GNSIS to open a web address.");
  }

  private async frontApp(): Promise<string | null> {
    const front = await this.shell.run("/usr/bin/lsappinfo", ["front"], { timeoutMs: 3_000 });
    const asn = front.stdout.trim();
    if (front.code !== 0 || !asn) return null;
    const info = await this.shell.run("/usr/bin/lsappinfo", ["info", "-only", "name", asn], { timeoutMs: 3_000 });
    const match = /"(?:LSDisplayName|name)"\s*=\s*"([^"]+)"/i.exec(info.stdout);
    return match ? match[1] : null;
  }

  async tabs(browser: { app: string; family: Family }): Promise<Tab[]> {
    const source =
      browser.family === "chromium"
        ? `const w = Application(${literal(browser.app)}).windows[0]; const a = w.activeTabIndex(); JSON.stringify(w.tabs().map((t, i) => ({ n: i + 1, title: t.title(), url: t.url(), active: i + 1 === a })))`
        : `const w = Application("Safari").windows[0]; const c = w.currentTab().index(); JSON.stringify(w.tabs().map((t, i) => ({ n: i + 1, title: t.name(), url: t.url() || "", active: i + 1 === c })))`;
    const out = await this.script(browser, source);
    try {
      const tabs = JSON.parse(out) as Tab[];
      return Array.isArray(tabs) ? tabs : [];
    } catch {
      return [];
    }
  }

  private async script(browser: { app: string }, source: string): Promise<string> {
    try {
      return await jxa(this.shell, source, browser.app);
    } catch (err) {
      throw scriptProblem(err, browser.app);
    }
  }
}

function hostOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url.slice(0, 60);
  }
}
