/**
 * `open`: an app, a web address, or a file or folder, the way the person
 * would open it themselves — `open` on macOS, which uses their default apps
 * and their own browser.
 *
 * Programs and installers found as files are not opened (a downloaded
 * .command or .pkg runs code); an app is opened by name, through the system's
 * own list of installed apps. Web addresses are only http and https.
 */
import path from "node:path";
import { promises as fs } from "node:fs";
import { ActionProblem, required, type ActionTool, type PreparedAction } from "../actions.js";
import type { FilesTool } from "../files.js";
import { applescript, literal, type Shell } from "./shell.js";

const RUNS_CODE = new Set([
  ".command", ".tool", ".terminal", ".sh", ".zsh", ".bash", ".pkg", ".mpkg",
  ".workflow", ".action", ".scpt", ".scptd", ".applescript", ".jar", ".app",
]);

export class OpenTool implements ActionTool {
  readonly name = "open";
  readonly platforms = ["darwin"] as const;

  constructor(
    private readonly shell: Shell,
    private readonly files: FilesTool,
    private readonly wait: (ms: number) => Promise<void> = (ms) => new Promise((r) => setTimeout(r, ms)),
  ) {}

  async prepare(args: Record<string, unknown>): Promise<PreparedAction> {
    const target = required(args, "target", "what to open");
    if (/^https?:\/\//i.test(target)) {
      const url = asWebAddress(target);
      if (!url) throw new ActionProblem("failed", `${target} is not a web address GNSIS can open.`);
      return this.prepareUrl(url);
    }
    if (/^[a-z][a-z0-9+.-]*:/i.test(target)) {
      throw new ActionProblem("refused", `GNSIS opens web addresses, apps, files and folders — not ${target.split(":")[0]}: links.`);
    }
    // An installed app wins over a folder that happens to share its name.
    const app = await this.installedApp(target);
    if (app) return this.prepareApp(app);
    const local = await this.files.resolve(target, "any").catch((err: unknown) => {
      // More than one match, or Finder refused: say so, rather than trying
      // the name as an app or a website instead.
      if (err instanceof ActionProblem && (err.status === "ambiguous" || err.status === "needs_permission")) throw err;
      return null;
    });
    if (local) return this.preparePath(local.path, local.scope.value);
    const bare = asWebAddress(target);
    if (bare) return this.prepareUrl(bare);
    return this.prepareApp(target);
  }

  /** The app's own name if it is installed in one of the usual places. */
  private async installedApp(name: string): Promise<string | null> {
    const wanted = `${name.replace(/\.app$/i, "")}.app`.toLowerCase();
    const home = process.env.HOME ?? "";
    for (const dir of ["/Applications", "/System/Applications", "/System/Applications/Utilities", path.join(home, "Applications")]) {
      const entries = await fs.readdir(dir).catch(() => [] as string[]);
      const hit = entries.find((entry) => entry.toLowerCase() === wanted);
      if (hit) return hit.slice(0, -".app".length);
    }
    return null;
  }

  private prepareUrl(url: URL): PreparedAction {
    const host = url.hostname.replace(/^www\./, "");
    return {
      tool: this.name,
      action: "url",
      effect: "open_remote",
      summary: `Open ${host} in your browser`,
      scope: [{ value: host, source: "named", kind: "site" }],
      run: async () => {
        const result = await this.shell.run("/usr/bin/open", [url.toString()]);
        if (result.code !== 0) throw new ActionProblem("failed", `The browser did not open ${host}.`);
        return { verified: "screen", message: `Asked your browser to open ${host}. Look at the screen to see it loaded.`, detail: { url: url.origin } };
      },
    };
  }

  private async preparePath(full: string, said: string): Promise<PreparedAction> {
    // Hidden, system and outside-home places stay closed, including through a link.
    await this.files.ensureAllowed(full, "open");
    const stat = await fs.stat(full);
    const ext = path.extname(full).toLowerCase();
    if (full.endsWith(".app") && stat.isDirectory()) return this.prepareApp(path.basename(full, ".app"));
    if (!stat.isDirectory() && RUNS_CODE.has(ext)) {
      throw new ActionProblem("refused", `${path.basename(full)} is a program or installer. GNSIS does not run those; open it yourself if you mean to.`);
    }
    const shown = this.files.show(full);
    return {
      tool: this.name,
      action: stat.isDirectory() ? "folder" : "file",
      effect: "open_local",
      summary: `Open ${shown}`,
      scope: [{ value: said, source: "named" }],
      run: async () => {
        const result = await this.shell.run("/usr/bin/open", [full]);
        if (result.code !== 0) throw new ActionProblem("failed", `macOS could not open ${shown}.`);
        return { verified: "screen", message: `Opened ${shown}.`, detail: { path: shown } };
      },
    };
  }

  private prepareApp(name: string): PreparedAction {
    return {
      tool: this.name,
      action: "app",
      effect: "open_local",
      summary: `Open ${name}`,
      scope: [{ value: name, source: "named" }],
      run: async () => {
        const result = await this.shell.run("/usr/bin/open", ["-a", name]);
        if (result.code !== 0) {
          throw new ActionProblem("not_found", `There is no app called ${name} on this Mac.`, { app: name });
        }
        // `open` returns once the launch is requested; check it is running.
        for (let i = 0; i < 20; i += 1) {
          const running = await applescript(this.shell, `application ${literal(name)} is running`, name).catch(() => "");
          if (running === "true") {
            return { verified: "app", message: `${name} is open.`, detail: { app: name, running: true } };
          }
          await this.wait(250);
        }
        return {
          verified: "screen",
          message: `Asked macOS to open ${name}, but it is not running yet. Look at the screen.`,
          detail: { app: name, running: false },
        };
      },
    };
  }
}

const FILE_ENDINGS = new Set([
  "pdf", "doc", "docx", "txt", "rtf", "md", "pages", "key", "numbers", "xls", "xlsx", "csv",
  "ppt", "pptx", "png", "jpg", "jpeg", "gif", "heic", "webp", "svg", "mp3", "mp4", "mov", "m4a",
  "wav", "zip", "dmg", "json", "html", "htm", "psd", "ai", "sketch", "fig", "epub",
]);

/** A web address, if that is what the target is; bare domains get https. */
export function asWebAddress(target: string): URL | null {
  const text = target.trim();
  if (/^https?:\/\//i.test(text)) {
    try {
      return new URL(text);
    } catch {
      return null;
    }
  }
  // A path, query or fragment may follow the domain, spaces and all
  // ("youtube.com/results?search_query=Andrew Tate"); URL encodes them.
  const bare = /^(www\.)?[a-z0-9-]+(\.[a-z0-9-]+)*\.([a-z]{2,})([/?#].*)?$/i.exec(text);
  // "report.pdf" is a file someone could not find, not a website.
  if (bare && !FILE_ENDINGS.has(bare[3].toLowerCase())) {
    try {
      return new URL(`https://${text}`);
    } catch {
      return null;
    }
  }
  return null;
}
