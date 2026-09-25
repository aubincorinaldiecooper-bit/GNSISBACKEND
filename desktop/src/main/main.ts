/**
 * GNSIS desktop main process — Electron edge only.
 *
 * Windows, permission prompts, global shortcuts, and IPC live here; call
 * epochs, protocol events, and socket ownership live in HostSession. The
 * renderer owns devices; the daemon never sees an Electron object.
 */
import { app, BrowserWindow, ipcMain } from "electron";
import path from "node:path";
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

const RUNTIME_URL = process.env.GNSIS_RUNTIME_URL ?? "http://127.0.0.1:8080";
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
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      sandbox: false,
      nodeIntegration: false,
    },
  });
  win.loadFile(path.join(__dirname, "../renderer/index.html"));
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
