/**
 * The Mac actions with macOS stood in for: what they would run, and what
 * they make of the answers. Real-Mac behaviour is proven on a Mac, not here.
 */
import assert from "node:assert/strict";
import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import { ActionProblem } from "./actions.js";
import { FilesTool } from "./files.js";
import { BrowserTool } from "./mac/browser.js";
import { scriptProblem } from "./mac/finder.js";
import { InputTool, normalizeCombo } from "./mac/input.js";
import { OpenTool, asWebAddress } from "./mac/open.js";
import { classifyScriptError, type Shell, type ShellResult } from "./mac/shell.js";

type Handler = (file: string, args: string[]) => Partial<ShellResult> | undefined;

function fakeShell(handler: Handler): Shell & { calls: Array<[string, string[]]>; timeouts: Array<number | undefined> } {
  const calls: Array<[string, string[]]> = [];
  const timeouts: Array<number | undefined> = [];
  return {
    calls,
    timeouts,
    async run(file, args, opts) {
      calls.push([file, args]);
      timeouts.push(opts?.timeoutMs);
      const result = handler(file, args) ?? {};
      return { code: 0, stdout: "", stderr: "", ...result };
    },
  };
}

const noWait = async () => {};

async function tmpHome(): Promise<string> {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "gnsis-mac-"));
  for (const dir of ["Desktop", "Documents", "Downloads"]) await fs.mkdir(path.join(root, dir));
  await fs.writeFile(path.join(root, "Downloads", "report.pdf"), "pdf");
  await fs.writeFile(path.join(root, "Downloads", "install.command"), "#!/bin/sh");
  return root;
}

test("open: a web address, a file, a folder and an app are told apart", async () => {
  const root = await tmpHome();
  const shell = fakeShell((_file, args) => (args.includes("is running") || args.some((a) => a.includes("is running")) ? { stdout: "true" } : undefined));
  const open = new OpenTool(shell, new FilesTool({ home: root }), noWait);

  const url = await open.prepare({ target: "https://github.com/anthropics" });
  assert.equal(url.effect, "open_remote");
  assert.deepEqual(url.scope, [{ value: "github.com", source: "named", kind: "site" }]);

  const file = await open.prepare({ target: "report.pdf" });
  assert.equal(file.effect, "open_local");
  assert.equal(file.summary, "Open ~/Downloads/report.pdf");

  const folder = await open.prepare({ target: "Downloads" });
  assert.equal(folder.summary, "Open ~/Downloads");

  const app = await open.prepare({ target: "Spotify" });
  assert.equal(app.action, "app");
  const done = await app.run();
  assert.deepEqual(shell.calls[0], ["/usr/bin/open", ["-a", "Spotify"]]);
  assert.equal(done.verified, "app");
});

test("open: a program found as a file is not run, and odd links are refused", async () => {
  const root = await tmpHome();
  const open = new OpenTool(fakeShell(() => undefined), new FilesTool({ home: root }), noWait);
  for (const target of ["install.command", "javascript:alert(1)", "file:///etc/passwd"]) {
    await assert.rejects(open.prepare({ target }), (err: unknown) => err instanceof ActionProblem && err.status === "refused", target);
  }
});

test("open: a missing app is reported as not found", async () => {
  const root = await tmpHome();
  const open = new OpenTool(fakeShell(() => ({ code: 1, stderr: "Unable to find application named 'Nope'" })), new FilesTool({ home: root }), noWait);
  const prepared = await open.prepare({ target: "Nope" });
  await assert.rejects(prepared.run(), (err: unknown) => err instanceof ActionProblem && err.status === "not_found");
});

test("a file name is not mistaken for a website", () => {
  assert.equal(asWebAddress("report.pdf"), null);
  assert.equal(asWebAddress("github.com")?.toString(), "https://github.com/");
  assert.equal(asWebAddress("not a url"), null);
});

test("macOS refusals are named for what the person has to switch on", () => {
  assert.deepEqual(classifyScriptError("execution error: Not authorized to send Apple events to Finder. (-1743)", "Finder"), { kind: "automation_denied", app: "Finder" });
  assert.deepEqual(classifyScriptError("System Events got an error: osascript is not allowed assistive access. (-25211)", "System Events"), { kind: "accessibility_denied" });
  const automation = scriptProblem({ kind: "automation_denied", app: "Google Chrome" }, "Google Chrome");
  assert.equal(automation.status, "needs_permission");
  assert.match(automation.message, /Automation/);
  assert.equal(automation.detail.app, "Google Chrome");
});

test("keys: combinations are normalized, and quitting is always asked about", async () => {
  assert.equal(normalizeCombo("Command+Shift+T"), "cmd+shift+t");
  assert.equal(normalizeCombo("⌘F"), "cmd+f");
  assert.equal(normalizeCombo("return"), "enter");
  assert.throws(() => normalizeCombo("hyper+x"));
  const shell = fakeShell(() => undefined);
  const input = new InputTool(shell, { bounds: () => ({ x: 0, y: 0, width: 1440, height: 900 }) });
  const find = await input.prepare({ action: "keys", keys: "cmd+f" });
  assert.equal(find.consequential, undefined);
  await find.run();
  assert.deepEqual(shell.calls[0], ["/usr/bin/osascript", ["-e", 'tell application "System Events" to keystroke "f" using {command down}']]);
  const quit = await input.prepare({ action: "keys", keys: "cmd+q" });
  assert.match(quit.consequential ?? "", /quits/);
  const enter = await input.prepare({ action: "keys", keys: "enter" });
  await enter.run();
  assert.deepEqual(shell.calls[1], ["/usr/bin/osascript", ["-e", 'tell application "System Events" to key code 36']]);
});

test("type: the text is passed as one quoted string, never spliced into a script", async () => {
  const shell = fakeShell(() => undefined);
  const input = new InputTool(shell, { bounds: () => ({ x: 0, y: 0, width: 100, height: 100 }) });
  const typed = await input.prepare({ action: "type", text: 'say "hi" \\ end' });
  await typed.run();
  assert.equal(shell.calls[0][1][1], 'tell application "System Events" to keystroke "say \\"hi\\" \\\\ end"');
});

test("click: by position maps onto the shared display; by name a send button is always asked about", async () => {
  const input = new InputTool(fakeShell(() => undefined), { bounds: () => ({ x: 0, y: 25, width: 1440, height: 900 }) });
  const at = await input.prepare({ action: "click", x: 0.5, y: 0.5 });
  assert.equal(at.summary, "Click at 50% across, 50% down the screen");
  const send = await input.prepare({ action: "click", label: "Send" });
  assert.ok(send.consequential);
  const play = await input.prepare({ action: "click", label: "Play" });
  assert.equal(play.consequential, undefined);
});

test("browser: goes to an address in the browser in front and checks it arrived", async () => {
  let url = "https://example.org/";
  const shell = fakeShell((file, args) => {
    if (file === "/usr/bin/lsappinfo" && args[0] === "front") return { stdout: "ASN:0x0-0x1234" };
    if (file === "/usr/bin/lsappinfo") return { stdout: '"LSDisplayName"="Google Chrome"' };
    const source = args[args.length - 1];
    if (source.includes("activeTab.url =")) {
      url = "https://github.com/";
      return { stdout: "" };
    }
    if (source.includes("JSON.stringify(w.tabs()")) {
      return { stdout: JSON.stringify([{ n: 1, title: "GitHub", url, active: true }]) };
    }
    return undefined;
  });
  const browser = new BrowserTool(shell, async () => {}, noWait);
  const go = await browser.prepare({ action: "go", url: "https://github.com" });
  assert.equal(go.effect, "open_remote");
  assert.equal(go.summary, "Go to github.com in Google Chrome");
  const done = await go.run();
  assert.equal(done.verified, "browser");
  assert.match(done.message, /on github\.com/);
});

test("browser: with no browser open, it says so instead of launching one", async () => {
  const shell = fakeShell((file, args) => {
    if (file === "/usr/bin/lsappinfo") return { code: 1 };
    if (args[args.length - 1].includes(".running()")) return { stdout: "false" };
    return undefined;
  });
  await assert.rejects(
    new BrowserTool(shell, async () => {}, noWait).prepare({ action: "tabs" }),
    (err: unknown) => err instanceof ActionProblem && err.status === "not_found",
  );
});

/** A browser with tabs, driven the way the tool drives it: by the scripts it sends. */
function fakeBrowser(app: "Google Chrome" | "Safari", opts: { ignoreNewTab?: boolean; windows?: boolean; inFront?: boolean; unreadable?: boolean } = {}) {
  let windows = opts.windows ?? true;
  const tabs: Array<{ title: string; url: string }> = windows ? [{ title: "Mail", url: "https://mail.example/" }] : [];
  let active = tabs.length;
  const scripts: string[] = [];
  const shell = fakeShell((file, args) => {
    if (file === "/usr/bin/lsappinfo" && args[0] === "front") return { stdout: "ASN:0x0-0x1234" };
    if (file === "/usr/bin/lsappinfo") return { stdout: `"LSDisplayName"="${opts.inFront === false ? "GNSIS" : app}"` };
    const source = args[args.length - 1];
    scripts.push(source);
    if (source.includes(".running()")) return { stdout: source.includes(app) ? "true" : "false" };
    if (source.includes("tabs.push") || source.includes("make()")) {
      if (opts.ignoreNewTab) return { stdout: "" };
      const url = /url: "([^"]+)"/.exec(source)?.[1] ?? (app === "Safari" ? "" : "chrome://newtab/");
      if (!windows) windows = true;
      tabs.push({ title: url ? "YouTube" : "New Tab", url });
      active = tabs.length;
      return { stdout: "" };
    }
    if (source.includes("JSON.stringify(w.tabs()")) {
      if (!windows) return { code: 1, stderr: "execution error: Error: Invalid index. (-1719)" };
      if (opts.unreadable && tabs.length > 1) return { code: 1, stderr: "Connection is invalid. (-609)" };
      return { stdout: JSON.stringify(tabs.map((t, i) => ({ n: i + 1, title: t.title, url: t.url, active: i + 1 === active }))) };
    }
    return undefined;
  });
  return { shell, tabs, scripts, active: () => active };
}

test("browser: new_tab opens a blank tab at the end and brings it forward, and checks it did", async () => {
  const b = fakeBrowser("Google Chrome");
  const tool = new BrowserTool(b.shell, async () => {}, noWait);
  const prepared = await tool.prepare({ action: "new_tab" });
  assert.equal(prepared.effect, "open_local", "a blank tab changes nothing and sends nothing away");
  assert.deepEqual(prepared.scope, []);
  assert.equal(prepared.summary, "Open a new tab in Google Chrome");
  const done = await prepared.run();
  assert.equal(b.tabs.length, 2, "one more tab than before");
  assert.equal(b.active(), 2, "and it is the one in front");
  assert.equal(done.verified, "browser");
  assert.equal(done.message, "Google Chrome opened a new tab; it now has 2 tabs.");
  assert.ok(b.scripts.some((src) => src.includes("w.tabs.push(app.Tab({}))") && src.includes("w.activeTabIndex = w.tabs.length")));
});

test("browser: new_tab at an address is judged like going there, and waits for the page", async () => {
  const b = fakeBrowser("Safari");
  const tool = new BrowserTool(b.shell, async () => {}, noWait);
  const prepared = await tool.prepare({ action: "new_tab", url: "youtube.com" });
  assert.equal(prepared.effect, "open_remote");
  assert.deepEqual(prepared.scope, [{ value: "youtube.com", source: "named", kind: "site" }]);
  assert.equal(prepared.summary, "Open youtube.com in a new tab in Safari");
  const done = await prepared.run();
  assert.equal(done.verified, "browser");
  assert.equal(done.message, "Safari opened a new tab at youtube.com; it now has 2 tabs.");
  const script = b.scripts.find((src) => src.includes("tabs.push"))!;
  assert.match(script, /app\.Tab\(\{ url: "https:\/\/youtube\.com\/" \}\)/, "the address is passed as one quoted string");
  assert.match(script, /w\.currentTab = w\.tabs\[w\.tabs\.length - 1\]/);
  await assert.rejects(tool.prepare({ action: "new_tab", url: "not a web address" }), /not a web address/);
});

test("browser: with no window open, new_tab makes one; a browser that opens nothing is reported, not assumed", async () => {
  const empty = fakeBrowser("Google Chrome", { windows: false });
  const done = await (await new BrowserTool(empty.shell, async () => {}, noWait).prepare({ action: "new_tab" })).run();
  assert.equal(empty.tabs.length, 1);
  assert.equal(done.verified, "browser");
  assert.ok(empty.scripts.some((src) => src.includes("app.Window().make()")));

  const stuck = fakeBrowser("Google Chrome", { ignoreNewTab: true });
  const prepared = await new BrowserTool(stuck.shell, async () => {}, noWait).prepare({ action: "new_tab" });
  await assert.rejects(prepared.run(), (err: unknown) => err instanceof ActionProblem && err.status === "failed" && /did not open a new tab/.test(err.message));
});

test("browser: a browser running in the background with no window is not woken up by new_tab", async () => {
  const b = fakeBrowser("Google Chrome", { windows: false, inFront: false });
  const prepared = await new BrowserTool(b.shell, async () => {}, noWait).prepare({ action: "new_tab", url: "youtube.com" });
  await assert.rejects(prepared.run(), (err: unknown) => err instanceof ActionProblem && /no window open/.test(err.message));
  assert.equal(b.tabs.length, 0, "no window was made");
  assert.ok(!b.scripts.some((src) => src.includes("make()")), "the window-making script never ran");
});

test("browser: a new tab that could not be checked is said so, never reported as not done", async () => {
  const b = fakeBrowser("Google Chrome", { unreadable: true });
  const done = await (await new BrowserTool(b.shell, async () => {}, noWait).prepare({ action: "new_tab" })).run();
  assert.equal(b.tabs.length, 2, "the tab was opened");
  assert.equal(done.verified, "screen");
  assert.match(done.message, /could not be checked/);
  // A browser that stops answering cannot hold the action open: the check
  // gives up after two failed reads, and each read is given 2 seconds.
  const reads = b.shell.calls.map(([, args], i) => ({ src: args[args.length - 1] ?? "", t: b.shell.timeouts[i] })).filter((c) => c.src.includes("JSON.stringify(w.tabs()"));
  assert.equal(reads.length, 3, "one read before, two failed reads after");
  assert.deepEqual(reads.slice(1).map((r) => r.t), [2000, 2000]);
});

test("browser: every action the shared catalog offers is one the tool can do", async () => {
  const { hostToolSchema } = await import("./catalog.js");
  const offered = hostToolSchema("browser")?.parameters.properties?.action.enum as string[];
  assert.ok(offered.includes("new_tab"));
  const b = fakeBrowser("Google Chrome");
  const tool = new BrowserTool(b.shell, async () => {}, noWait);
  for (const action of offered) {
    const prepared = await tool.prepare({ action, tab: 1, url: "youtube.com" });
    assert.equal(prepared.action, action, `${action} is handled`);
  }
  await assert.rejects(tool.prepare({ action: "teleport" }), (err: unknown) => err instanceof ActionProblem && err.status === "unsupported");
});

test("a web address may carry a search, spaces and all", () => {
  assert.equal(asWebAddress("youtube.com/results?search_query=Andrew Tate")?.toString(), "https://youtube.com/results?search_query=Andrew%20Tate");
  assert.equal(asWebAddress("youtube.com?q=x")?.toString(), "https://youtube.com/?q=x");
  assert.equal(asWebAddress("report.pdf?x=1"), null, "still a file, not a website");
  assert.equal(asWebAddress("youtube.com now"), null);
  assert.equal(asWebAddress("Andrew Tate"), null);
});
