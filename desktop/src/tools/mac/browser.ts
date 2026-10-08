/**
 * Browser control backed entirely by Cua Driver.
 *
 * Chrome and Edge prefer Cua's exact CDP browser binding. Other browsers use
 * Cua's native accessibility/keyboard path. No extension and no JXA/Apple
 * Events browser actuator are involved.
 */
import { ActionProblem, required, str, type ActionTool, type PreparedAction } from "../actions.js";
import { asWebAddress } from "./open.js";
import {
  CuaToolError,
  collectTabLabels,
  listWindows,
  windowArgs,
  type CuaControl,
  type CuaResponse,
  type CuaWindow,
} from "../cua/driver.js";

type Browser = CuaWindow & { family: "typed" | "native" };
const BROWSER_SESSION = "gnsis-browser";
const TYPED_BROWSERS = new Set(["Google Chrome", "Microsoft Edge"]);
const BROWSER_NAMES = ["Google Chrome", "Microsoft Edge", "Safari", "Arc", "Brave Browser", "Chromium", "Vivaldi", "Firefox"];

export interface Tab {
  n: number;
  title: string;
  url: string;
  active: boolean;
  id?: string;
}

interface Binding {
  targetId: string;
  tabs: Tab[];
}

export class BrowserTool implements ActionTool {
  readonly name = "browser";
  readonly platforms = ["darwin"] as const;

  constructor(
    private readonly cua: CuaControl,
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
          summary: `List the tabs open in ${browser.appName}`,
          scope: [],
          run: async () => {
            const tabs = await this.tabs(browser);
            return {
              verified: browser.family === "typed" ? "browser" : "screen",
              message: `${browser.appName} has ${tabs.length} visible tab candidate${tabs.length === 1 ? "" : "s"}.`,
              detail: { browser: browser.appName, tabs: tabs.slice(0, 15).map((t) => `${t.n}${t.active ? "*" : ""}. ${t.title.slice(0, 60)}${t.url ? ` — ${hostOf(t.url)}` : ""}`) },
            };
          },
        };
      case "switch": {
        const n = Number(args.tab);
        if (!Number.isInteger(n) || n < 1 || n > 100) throw new ActionProblem("failed", "Say which tab number.");
        return {
          tool: this.name,
          action,
          effect: "open_local",
          summary: `Switch ${browser.appName} to tab ${n}`,
          scope: [],
          run: async () => {
            await this.hotkey(browser, ["cmd", String(Math.min(n, 9))]);
            await this.wait(150);
            return { verified: "screen", message: `Asked ${browser.appName} to switch to tab ${n}. Panoptic will verify the visible tab.` };
          },
        };
      }
      case "new_tab": {
        const given = typeof args.url === "string" && args.url.trim() ? args.url : null;
        const url = given ? asWebAddress(given) : null;
        if (given && !url) throw new ActionProblem("failed", "That is not a web address.");
        const host = url ? url.hostname.replace(/^www\./, "") : null;
        return {
          tool: this.name,
          action,
          effect: url ? "open_remote" : "open_local",
          summary: host ? `Open ${host} in a new tab in ${browser.appName}` : `Open a new tab in ${browser.appName}`,
          scope: host ? [{ value: host, source: "named", kind: "site" }] : [],
          run: async () => {
            await this.hotkey(browser, ["cmd", "t"]);
            await this.wait(120);
            if (url) await this.go(browser, url.toString());
            return { verified: "screen", message: host ? `Opened a new tab for ${host} in ${browser.appName}.` : `Opened a new tab in ${browser.appName}.`, detail: { url: host } };
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
          summary: `Go to ${host} in ${browser.appName}`,
          scope: [{ value: host, source: "named", kind: "site" }],
          run: async () => {
            const route = await this.go(browser, url.toString());
            return { verified: route === "typed" ? "browser" : "screen", message: `${browser.appName} is going to ${host} via Cua ${route === "typed" ? "browser binding" : "native control"}.`, detail: { url: host, route } };
          },
        };
      }
      case "back":
      case "forward":
      case "reload":
        return {
          tool: this.name,
          action,
          effect: "open_local",
          summary: `${action === "reload" ? "Reload" : action === "back" ? "Go back" : "Go forward"} in ${browser.appName}`,
          scope: [],
          run: async () => {
            await this.hotkey(browser, action === "reload" ? ["cmd", "r"] : action === "back" ? ["cmd", "["] : ["cmd", "]"]);
            return { verified: "screen", message: `Done in ${browser.appName}. Panoptic will verify the visible page.` };
          },
        };
      case "prepare":
        if (browser.family !== "typed") {
          throw new ActionProblem("unsupported", "Cua's typed browser binding currently supports Chrome and Edge.");
        }
        return {
          tool: this.name,
          action,
          effect: "input",
          summary: `Connect GNSIS to this ${browser.appName} profile for extensionless typed browser control`,
          scope: [{ value: browser.appName, source: "named" }],
          consequential: "attaches to the signed-in browser profile and enables its local DevTools connection",
          needs: [{ kind: "accessibility" }],
          run: async () => {
            if (!this.cua.withExistingProfileAuthorization) {
              throw new ActionProblem("unsupported", "This Cua host does not expose protected existing-profile authorization.");
            }
            await this.cua.withExistingProfileAuthorization(browser.pid, browser.windowId, async () => {
              await this.call("browser_prepare", {
                ...windowArgs(browser),
                session: BROWSER_SESSION,
                strategy: { kind: "existing_profile" },
              });
            });
            const binding = await this.bind(browser, true);
            return {
              verified: "browser",
              message: `Connected ${browser.appName} to Cua's extensionless typed browser route.`,
              detail: { tabs: binding.tabs.length },
            };
          },
        };
      case "inspect":
        return {
          tool: this.name,
          action,
          effect: "read",
          summary: `Inspect the active page in ${browser.appName}`,
          scope: [],
          run: async () => {
            if (browser.family !== "typed") throw new ActionProblem("unsupported", "Semantic browser inspection is currently available for Chrome and Edge; Panoptic still sees other browsers visually.");
            const binding = await this.bind(browser, true);
            const active = activeTab(binding.tabs);
            if (!active?.id) throw new ActionProblem("failed", "Cua could not identify the active tab.");
            const state = await this.call("get_browser_state", { session: BROWSER_SESSION, target_id: binding.targetId, tab_id: active.id, format: "semantic_v2" });
            return { verified: "browser", message: `Read the active ${browser.appName} page through Cua's exact browser binding.`, detail: { target_id: binding.targetId, tab_id: active.id, state: state.structured } };
          },
        };
      case "upload": {
        const ref = required(args, "ref", "which page element should receive the files");
        const files = Array.isArray(args.files) ? args.files.filter((v): v is string => typeof v === "string" && v.length > 0) : [];
        if (!files.length) throw new ActionProblem("failed", "Say which files to upload.");
        return {
          tool: this.name,
          action,
          effect: "open_remote",
          summary: `Upload ${files.length} file${files.length === 1 ? "" : "s"} in ${browser.appName}`,
          scope: files.map((value) => ({ value, source: "named" as const })),
          consequential: "uploading files sends local data to a website",
          run: async () => {
            const { targetId, tab } = await this.activeTyped(browser);
            await this.call("browser_set_input_files", { session: BROWSER_SESSION, target_id: targetId, tab_id: tab.id, ref, files });
            return { verified: "screen", message: "Attached the requested file(s). Panoptic will verify the page state." };
          },
        };
      }
      case "download": {
        const ref = required(args, "ref", "which page element starts the download");
        const destination = required(args, "destination_root", "where to save the download");
        return {
          tool: this.name,
          action,
          effect: "change",
          summary: `Download the selected item into ${destination}`,
          scope: [{ value: destination, source: "named" }],
          run: async () => {
            const { targetId, tab } = await this.activeTyped(browser);
            const result = await this.call("browser_download", { session: BROWSER_SESSION, target_id: targetId, tab_id: tab.id, ref, destination_root: destination });
            return { verified: "browser", message: "The browser download completed through Cua.", detail: { result: result.structured } };
          },
        };
      }
      case "dialog": {
        const dialogAction = str(args, "dialog_action") ?? "inspect";
        if (!["inspect", "accept", "dismiss"].includes(dialogAction)) throw new ActionProblem("failed", "dialog_action must be inspect, accept, or dismiss.");
        return {
          tool: this.name,
          action,
          effect: dialogAction === "inspect" ? "read" : "input",
          summary: `${dialogAction === "inspect" ? "Inspect" : dialogAction === "accept" ? "Accept" : "Dismiss"} the page dialog in ${browser.appName}`,
          scope: [],
          consequential: dialogAction === "inspect" ? undefined : "resolving a page dialog may confirm or cancel an action",
          run: async () => {
            const { targetId, tab } = await this.activeTyped(browser);
            const result = await this.call("browser_dialog", {
              session: BROWSER_SESSION,
              target_id: targetId,
              tab_id: tab.id,
              action: dialogAction,
              ...(str(args, "dialog_id") ? { dialog_id: str(args, "dialog_id") } : {}),
              ...(typeof args.prompt_text === "string" ? { prompt_text: args.prompt_text } : {}),
            });
            return { verified: dialogAction === "inspect" ? "browser" : "screen", message: `Browser dialog ${dialogAction} completed.`, detail: { dialog: result.structured } };
          },
        };
      }
      default:
        throw new ActionProblem("unsupported", `browser cannot ${String(action)}.`);
    }
  }

  private async pick(): Promise<Browser> {
    const windows = await listWindows(this.cua, { onScreenOnly: true }).catch((e) => { throw problem(e); });
    const candidates = windows.filter((w) => BROWSER_NAMES.includes(w.appName));
    if (!candidates.length) throw new ActionProblem("not_found", "No web browser is open. Open one, or ask GNSIS to open a web address.");
    const withZ = candidates.filter((w) => w.zIndex !== null);
    const picked = withZ.length ? withZ.sort((a, b) => (b.zIndex ?? -1) - (a.zIndex ?? -1))[0] : candidates[0];
    return { ...picked, family: TYPED_BROWSERS.has(picked.appName) ? "typed" : "native" };
  }

  private async tabs(browser: Browser): Promise<Tab[]> {
    if (browser.family === "typed") {
      const binding = await this.bind(browser, false).catch(() => null);
      if (binding) return binding.tabs;
    }
    const state = await this.call("get_window_state", {
      ...windowArgs(browser),
      query: "tab",
      include_accessibility_tree: true,
      include_screenshot: false,
      max_elements: 300,
    });
    const labels = collectTabLabels(state.structured);
    if (labels.length) return labels.map((t, i) => ({ n: i + 1, title: t.title, url: "", active: t.selected }));
    return [{ n: 1, title: browser.title || browser.appName, url: "", active: true }];
  }

  private async bind(browser: Browser, requireTyped: boolean): Promise<Binding> {
    if (browser.family !== "typed") throw new ActionProblem("unsupported", "This browser does not expose Cua's typed browser route.");
    const args = { ...windowArgs(browser), session: BROWSER_SESSION };
    let response: CuaResponse;
    try {
      response = await this.cua.call("get_browser_state", args);
    } catch (error) {
      if (error instanceof CuaToolError && error.code === "browser_requires_setup") {
        if (requireTyped) {
          throw new ActionProblem(
            "needs_permission",
            `${browser.appName} needs a one-time approved Cua profile connection before typed browser tools can be used. Ask GNSIS to connect this browser.`,
            { provider: "cua", code: error.code, action: "browser.prepare" },
          );
        }
        throw error;
      }
      throw problem(error);
    }
    const root = asRecord(response.structured);
    const targetId = text(root?.target_id ?? root?.targetId);
    const rawTabs = Array.isArray(root?.tabs) ? root!.tabs as unknown[] : [];
    const tabs = rawTabs.flatMap((value, i) => {
      const t = asRecord(value);
      if (!t) return [];
      const id = text(t.tab_id ?? t.tabId ?? t.id);
      return [{
        n: i + 1,
        title: text(t.title) || `Tab ${i + 1}`,
        url: text(t.url),
        active: t.selected === true || t.active === true,
        id: id || undefined,
      }];
    });
    if (!targetId || !tabs.length) throw new ActionProblem("failed", "Cua bound the browser but did not return a usable target and tab list.");
    return { targetId, tabs };
  }

  private async activeTyped(browser: Browser): Promise<{ targetId: string; tab: Tab & { id: string } }> {
    if (browser.family !== "typed") throw new ActionProblem("unsupported", "Uploads, downloads, dialogs and semantic inspection require Chrome or Edge right now.");
    const binding = await this.bind(browser, true);
    const tab = activeTab(binding.tabs);
    if (!tab?.id) throw new ActionProblem("failed", "Cua could not identify the active browser tab.");
    return { targetId: binding.targetId, tab: tab as Tab & { id: string } };
  }

  private async go(browser: Browser, url: string): Promise<"typed" | "native"> {
    if (browser.family === "typed") {
      try {
        const { targetId, tab } = await this.activeTyped(browser);
        await this.call("browser_navigate", { session: BROWSER_SESSION, target_id: targetId, tab_id: tab.id, url });
        return "typed";
      } catch {
        // Exact CDP binding is preferred, but ordinary navigation must remain
        // available when existing-profile attachment is not yet authorized or
        // the browser exposes no typed route. Native Cua is still extensionless
        // and remains inside the same GNSIS approval/verification path.
      }
    }
    await this.hotkey(browser, ["cmd", "l"]);
    await this.call("type_text", { ...windowArgs(browser), text: url, delivery_mode: "foreground" });
    await this.call("press_key", { ...windowArgs(browser), key: "enter", delivery_mode: "foreground" });
    return "native";
  }

  private async hotkey(browser: Browser, keys: string[]): Promise<void> {
    await this.call("hotkey", { ...windowArgs(browser), keys, delivery_mode: "foreground" });
  }

  private async call(tool: string, args: Record<string, unknown>): Promise<CuaResponse> {
    try {
      return await this.cua.call(tool, args);
    } catch (error) {
      throw problem(error);
    }
  }
}

function activeTab(tabs: Tab[]): Tab | undefined {
  return tabs.find((t) => t.active) ?? tabs[0];
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function hostOf(url: string): string {
  try { return new URL(url).hostname.replace(/^www\./, ""); } catch { return url.slice(0, 60); }
}

function problem(error: unknown): ActionProblem {
  if (error instanceof ActionProblem) return error;
  if (error instanceof CuaToolError) {
    if (error.code?.includes("consent") || error.code?.includes("permission")) {
      return new ActionProblem("needs_permission", error.message, { provider: "cua", code: error.code });
    }
    if (error.code?.includes("not_found")) return new ActionProblem("not_found", error.message, { provider: "cua", code: error.code });
    if (error.code?.includes("unsupported") || error.code?.includes("route_unavailable")) return new ActionProblem("unsupported", error.message, { provider: "cua", code: error.code });
    return new ActionProblem("failed", error.message, { provider: "cua", code: error.code });
  }
  return new ActionProblem("failed", String((error as Error)?.message ?? error), { provider: "cua" });
}
