/**
 * GNSIS desktop main process — Electron edge only.
 *
 * Windows, permission prompts, global shortcuts, and IPC live here; call
 * epochs, protocol events, and socket ownership live in HostSession. The
 * renderer owns devices; the daemon never sees an Electron object.
 */
import { app, BrowserWindow, ipcMain } from "electron";
import path from "node:path";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { HostSession } from "../host/hostSession.js";
import {
  ElectronNotifications,
  ElectronPermissions,
  ElectronScreenshots,
  ElectronShortcuts,
} from "../host/electronMain.js";
import type { HostEvent } from "../host/protocol.js";
import { ToolRegistry } from "../tools/registry.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// Runtime resolution: GNSIS_RUNTIME_URL env var first (dev/CI), then a
// persisted desktop setting at <userData>/gnsis.json ({"runtimeUrl": ...}),
// then the local default. Packaged builds can't rely on env, so the JSON
// file is the supported seam for pointing the installed app at any runtime
// — including the future LocalGNSISProvider on 127.0.0.1.
function resolveRuntimeUrl(): string {
  const env = process.env.GNSIS_RUNTIME_URL;
  if (env) return env;
  try {
    const cfgPath = path.join(app.getPath("userData"), "gnsis.json");
    const cfg = JSON.parse(readFileSync(cfgPath, "utf8")) as {
      runtimeUrl?: unknown;
    };
    if (typeof cfg.runtimeUrl === "string" && cfg.runtimeUrl) {
      return cfg.runtimeUrl;
    }
  } catch {
    // no config file / unreadable — fall through to the default
  }
  return "http://127.0.0.1:8080";
}

const RUNTIME_URL = resolveRuntimeUrl();
const HOST_ID = process.env.GNSIS_HOST_ID ?? `host-${process.pid}`;
const SESSION_ID = process.env.GNSIS_SESSION_ID ?? HOST_ID;

let win: BrowserWindow | null = null;
const permissions = new ElectronPermissions();
const shortcuts = new ElectronShortcuts();
const notifications = new ElectronNotifications();
const screenshots = new ElectronScreenshots();
const tools = new ToolRegistry({ runtimeUrl: RUNTIME_URL });

const sendToRenderer = (channel: string, ...args: unknown[]) =>
  win?.webContents.send(channel, ...args);

const host = new HostSession({
  runtimeUrl: RUNTIME_URL,
  hostId: HOST_ID,
  chassis: "electron",
  capabilities: {
    mic: true,
    camera: true,
    screen: true,
    playback_ack: true,
    screenshots: true,
    global_shortcuts: true,
    notifications: true,
  },
  onControl: (c) => sendToRenderer("duplex:control", c),
  onAudio: (pcm) => sendToRenderer("duplex:audio", pcm),
  onClosed: (code) => sendToRenderer("duplex:closed", code, ""),
});

function createWindow(): void {
  win = new BrowserWindow({
    width: 960,
    height: 640,
    webPreferences: {
      preload: path.join(__dirname, "preload.mjs"),
      contextIsolation: true,
      sandbox: false,
      nodeIntegration: false,
    },
  });
  win.loadFile(path.join(__dirname, "../renderer/index.html"));
  win.on("closed", () => {
    win = null;
  });
}

// One HostSession per machine: a second GNSIS.app instance would open a
// second duplex socket and duplicate capture/shortcut state.
if (!app.requestSingleInstanceLock()) {
  app.quit();
}

app.whenReady().then(async () => {
  createWindow();
  host.connect(SESSION_ID);
  host.ready();

  shortcuts.register("Control+Alt+Space", () => {
    host.interrupt("global_shortcut");
    sendToRenderer("ui:interrupted");
  });

  ipcMain.handle("media:permissions", async () => ({
    microphone: await permissions.status("microphone"),
    camera: await permissions.status("camera"),
    screen: await permissions.status("screen"),
  }));
  ipcMain.handle("media:request", (_e, kind) => permissions.request(kind));
  ipcMain.handle("screenshot:capture", () => screenshots.capture());

  ipcMain.on("duplex:control", (_e, control) => {
    if ((control as { type?: string })?.type === "stop") host.endCall("client_stop");
    else host.emit(control as HostEvent);
  });
  ipcMain.on("host:event", (_e, event) => host.emit(event as HostEvent));
  ipcMain.on("call:start", () => host.startCall());
  ipcMain.on("duplex:audioFrame", (_e, header, pcm: Uint8Array) =>
    host.sendAudioFrame(header, Buffer.from(pcm)),
  );
  ipcMain.on("screen:frame", (_e, metadata, payload: Uint8Array) =>
    host.sendScreenFrame(metadata, Buffer.from(payload)),
  );
  ipcMain.handle("tool:call", (_e, tool: string, args: unknown) =>
    tools.call(tool, args as Record<string, unknown>),
  );
});

app.on("will-quit", () => {
  shortcuts.unregisterAll();
  host.disconnect("app_quit");
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("activate", () => {
  // macOS: closing the window keeps the app alive; a dock click must bring a
  // usable window back rather than leaving a windowless Host.
  if (win === null) createWindow();
});

app.on("second-instance", () => {
  if (win !== null) {
    if (win.isMinimized()) win.restore();
    win.focus();
  } else {
    createWindow();
  }
});
