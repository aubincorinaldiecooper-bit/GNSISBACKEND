/**
 * GNSIS desktop main process: window lifecycle, OS capture permissions,
 * global shortcut, and the IPC boundary between the renderer's device access
 * (getUserMedia/desktopCapturer live in the renderer) and the runtime sockets
 * (held here so there is exactly one).
 */
import {
  app,
  BrowserWindow,
  globalShortcut,
  ipcMain,
  session as electronSession,
  systemPreferences,
} from "electron";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { DuplexClient, ScreenClient } from "./wsClient.js";
import { ToolRegistry } from "../tools/registry.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const RUNTIME_URL = process.env.GNSIS_RUNTIME_URL ?? "http://127.0.0.1:8080";
const SESSION_ID = process.env.GNSIS_SESSION_ID ?? `desktop-${process.pid}`;

let win: BrowserWindow | null = null;
let duplex: DuplexClient | null = null;
let screenWs: ScreenClient | null = null;
const tools = new ToolRegistry({ runtimeUrl: RUNTIME_URL });

function sendToRenderer(channel: string, ...args: unknown[]): void {
  win?.webContents.send(channel, ...args);
}

function connect(): void {
  duplex?.close();
  screenWs?.close();
  duplex = new DuplexClient({ url: RUNTIME_URL, sessionId: SESSION_ID });
  screenWs = new ScreenClient({ url: RUNTIME_URL, sessionId: SESSION_ID });
  duplex.on("control", (c) => sendToRenderer("duplex:control", c));
  duplex.on("audio", (pcm) => sendToRenderer("duplex:audio", pcm));
  duplex.on("close", (code, reason) => sendToRenderer("duplex:closed", code, reason));
  duplex.on("error", (e) => sendToRenderer("duplex:error", String(e)));
  duplex.connect();
  screenWs.connect();
}

async function checkMediaPermissions(): Promise<Record<string, unknown>> {
  if (process.platform !== "darwin") return { platform: process.platform };
  return {
    mic: systemPreferences.getMediaAccessStatus("microphone"),
    camera: systemPreferences.getMediaAccessStatus("camera"),
    screen: systemPreferences.getMediaAccessStatus("screen"),
  };
}

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

  electronSession.defaultSession.setPermissionRequestHandler(
    (_wc, _permission, callback) => callback(true),
  );

  win.loadFile(path.join(__dirname, "../renderer/index.html"));
}

app.whenReady().then(() => {
  createWindow();
  connect();

  globalShortcut.register("Control+Alt+Space", () => {
    duplex?.sendControl({ type: "break", reason: "global_shortcut" });
    sendToRenderer("ui:interrupted");
  });

  ipcMain.handle("media:permissions", checkMediaPermissions);

  ipcMain.on("duplex:control", (_e, control) => duplex?.sendControl(control));
  ipcMain.on("duplex:audioFrame", (_e, header, pcm: Uint8Array) => {
    duplex?.sendAudioFrame(header, Buffer.from(pcm));
  });
  ipcMain.on("screen:frame", (_e, metadata, payload: Uint8Array) => {
    screenWs?.sendFrame(metadata, Buffer.from(payload));
  });
  ipcMain.handle("tool:call", async (_e, tool: string, args: unknown) => {
    return tools.call(tool, args as Record<string, unknown>);
  });
});

app.on("will-quit", () => {
  globalShortcut.unregisterAll();
  duplex?.sendControl({ type: "stop" });
  duplex?.close();
  screenWs?.close();
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
