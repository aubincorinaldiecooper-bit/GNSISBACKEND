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
import { WindowTool } from "./mac/window.js";
import { ClipboardTool } from "./mac/clipboard.js";
import { scriptProblem } from "./mac/finder.js";
import { InputTool, normalizeCombo } from "./mac/input.js";
import type { CuaControl, CuaResponse } from "./cua/driver.js";
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

function fakeCua(handler: (tool: string, args: Record<string, unknown>) => CuaResponse | Promise<CuaResponse>): CuaControl & { calls: Array<{ tool: string; args: Record<string, unknown> }> } {
  const calls: Array<{ tool: string; args: Record<string, unknown> }> = [];
  return {
    calls,
    async call(tool, args) {
      calls.push({ tool, args });
      return await handler(tool, args);
    },
  };
}

const oneWindow = {
  windows: [{
    pid: 42,
    window_id: 7,
    app_name: "TextEdit",
    title: "Notes",
    bounds: { x: 0, y: 25, width: 1000, height: 700 },
    z_index: 10,
    is_on_screen: true,
  }],
};

test("keys: combinations are normalized and dispatched through Cua", async () => {
  assert.equal(normalizeCombo("Command+Shift+T"), "cmd+shift+t");
  assert.equal(normalizeCombo("⌘F"), "cmd+f");
  assert.equal(normalizeCombo("return"), "enter");
  assert.throws(() => normalizeCombo("hyper+x"));

  const cua = fakeCua((tool) => tool === "list_windows" ? { text: "", structured: oneWindow } : { text: "ok" });
  const input = new InputTool(cua, { bounds: () => ({ x: 0, y: 0, width: 1440, height: 900 }) });

  const find = await input.prepare({ action: "keys", keys: "cmd+f" });
  assert.equal(find.consequential, undefined);
  await find.run();
  assert.deepEqual(cua.calls.at(-1), {
    tool: "hotkey",
    args: { pid: 42, window_id: 7, keys: ["cmd", "f"], delivery_mode: "foreground" },
  });

  const quit = await input.prepare({ action: "keys", keys: "cmd+q" });
  assert.match(quit.consequential ?? "", /quits/);

  const enter = await input.prepare({ action: "keys", keys: "enter" });
  await enter.run();
  assert.deepEqual(cua.calls.at(-1), {
    tool: "press_key",
    args: { pid: 42, window_id: 7, key: "enter", delivery_mode: "foreground" },
  });
});

test("type: text is handed to Cua as data, not a generated script", async () => {
  const cua = fakeCua((tool) => tool === "list_windows" ? { text: "", structured: oneWindow } : { text: "ok" });
  const input = new InputTool(cua, { bounds: () => ({ x: 0, y: 0, width: 100, height: 100 }) });
  const typed = await input.prepare({ action: "type", text: 'say "hi" \\ end' });
  await typed.run();
  assert.equal(cua.calls.at(-1)?.tool, "type_text");
  assert.equal(cua.calls.at(-1)?.args.text, 'say "hi" \\ end');
});

test("click: label uses a fresh no-screenshot AX snapshot; coordinates use desktop Cua input", async () => {
  const cua = fakeCua((tool) => {
    if (tool === "list_windows") return { text: "", structured: oneWindow };
    if (tool === "get_window_state") {
      return { text: "", structured: { elements: [{ role: "button", label: "Send", element_token: "s00000001:4" }] } };
    }
    return { text: "ok" };
  });
  const input = new InputTool(cua, { bounds: () => ({ x: 0, y: 25, width: 1440, height: 900 }) });

  const send = await input.prepare({ action: "click", label: "Send" });
  assert.ok(send.consequential);
  await send.run();
  const snapshot = cua.calls.find((c) => c.tool === "get_window_state")!;
  assert.equal(snapshot.args.include_screenshot, false);
  assert.deepEqual(cua.calls.at(-1), {
    tool: "click",
    args: { pid: 42, window_id: 7, element_token: "s00000001:4", button: "left", delivery_mode: "background" },
  });

  const at = await input.prepare({ action: "right_click", x: 0.5, y: 0.5 });
  await at.run();
  assert.equal(cua.calls.at(-1)?.tool, "click");
  assert.equal(cua.calls.at(-1)?.args.scope, "desktop");
  assert.equal(cua.calls.at(-1)?.args.button, "right");
});

test("input: scroll and drag route through Cua without a screenshot request", async () => {
  const cua = fakeCua((tool) => tool === "list_windows" ? { text: "", structured: oneWindow } : { text: "ok" });
  const input = new InputTool(cua, { bounds: () => ({ x: 0, y: 0, width: 1000, height: 800 }) });

  await (await input.prepare({ action: "scroll", direction: "down", amount: 4, app: "TextEdit" })).run();
  assert.equal(cua.calls.at(-1)?.tool, "scroll");

  await (await input.prepare({ action: "drag", from_x: 0.1, from_y: 0.2, to_x: 0.8, to_y: 0.7 })).run();
  assert.deepEqual(cua.calls.at(-1), {
    tool: "drag",
    args: { scope: "desktop", from_x: 100, from_y: 160, to_x: 800, to_y: 560, duration_ms: 350 },
  });
});

test("browser: Chrome navigation prefers Cua's exact browser binding", async () => {
  const cua = fakeCua((tool, args) => {
    if (tool === "list_windows") return {
      text: "",
      structured: { windows: [{ pid: 81, window_id: 9, app_name: "Google Chrome", title: "GitHub", z_index: 20, is_on_screen: true }] },
    };
    if (tool === "get_browser_state") return {
      text: "",
      structured: {
        target_id: "bt-1",
        tabs: [{ tab_id: "tab-1", title: "GitHub", url: "https://example.org/", selected: true }],
      },
    };
    if (tool === "browser_navigate") {
      assert.equal(args.target_id, "bt-1");
      assert.equal(args.tab_id, "tab-1");
      return { text: "navigated" };
    }
    return { text: "ok" };
  });
  const browser = new BrowserTool(cua, noWait);
  const go = await browser.prepare({ action: "go", url: "https://github.com" });
  const done = await go.run();
  assert.equal(done.verified, "browser");
  assert.ok(cua.calls.some((c) => c.tool === "browser_navigate"));
  assert.ok(!cua.calls.some((c) => c.tool === "hotkey" && c.args.keys?.[1] === "l"));
});

test("browser: Safari remains extensionless by using Cua native keyboard/text control", async () => {
  const cua = fakeCua((tool) => {
    if (tool === "list_windows") return {
      text: "",
      structured: { windows: [{ pid: 82, window_id: 10, app_name: "Safari", title: "Start Page", z_index: 20, is_on_screen: true }] },
    };
    return { text: "ok" };
  });
  const browser = new BrowserTool(cua, noWait);
  const go = await browser.prepare({ action: "go", url: "https://github.com" });
  const done = await go.run();
  assert.equal(done.verified, "screen");
  assert.deepEqual(cua.calls.slice(-3).map((c) => c.tool), ["hotkey", "type_text", "press_key"]);
});

test("browser: upload and dialog capabilities are exposed through the typed binding", async () => {
  const cua = fakeCua((tool) => {
    if (tool === "list_windows") return {
      text: "",
      structured: { windows: [{ pid: 81, window_id: 9, app_name: "Microsoft Edge", title: "Upload", z_index: 20, is_on_screen: true }] },
    };
    if (tool === "get_browser_state") return {
      text: "",
      structured: { target_id: "bt-2", tabs: [{ tab_id: "tab-2", title: "Upload", url: "https://example.org/", selected: true }] },
    };
    return { text: "ok", structured: { ok: true } };
  });
  const browser = new BrowserTool(cua, noWait);
  const upload = await browser.prepare({ action: "upload", ref: "p1:3", files: ["/tmp/a.pdf"] });
  assert.ok(upload.consequential);
  await upload.run();
  assert.equal(cua.calls.at(-1)?.tool, "browser_set_input_files");

  const dialog = await browser.prepare({ action: "dialog", dialog_action: "inspect" });
  await dialog.run();
  assert.equal(cua.calls.at(-1)?.tool, "browser_dialog");
});

test("window and clipboard capabilities use Cua tools", async () => {
  const cua = fakeCua((tool) => {
    if (tool === "list_windows") return { text: "", structured: oneWindow };
    if (tool === "clipboard_read") return { text: "", structured: { types: ["public.utf8-plain-text"], text: "hello" } };
    return { text: "ok" };
  });

  const windows = new WindowTool(cua);
  await (await windows.prepare({ action: "focus", app: "TextEdit" })).run();
  assert.equal(cua.calls.at(-1)?.tool, "bring_to_front");

  await (await windows.prepare({ action: "move_resize", app: "TextEdit", x: 20, y: 30, width: 800, height: 600 })).run();
  assert.equal(cua.calls.at(-1)?.tool, "set_window_frame");

  const clipboard = new ClipboardTool(cua);
  await (await clipboard.prepare({ action: "read", include_text: true })).run();
  assert.equal(cua.calls.at(-1)?.tool, "clipboard_read");
  await (await clipboard.prepare({ action: "write", text: "hello" })).run();
  assert.equal(cua.calls.at(-1)?.tool, "clipboard_write");
});
