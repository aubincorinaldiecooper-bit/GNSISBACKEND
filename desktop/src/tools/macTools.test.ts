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

function fakeShell(handler: Handler): Shell & { calls: Array<[string, string[]]> } {
  const calls: Array<[string, string[]]> = [];
  return {
    calls,
    async run(file, args) {
      calls.push([file, args]);
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
  assert.deepEqual(url.scope, [{ value: "github.com", source: "named" }]);

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
