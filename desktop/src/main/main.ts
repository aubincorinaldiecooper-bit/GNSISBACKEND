/**
 * GNSIS desktop main process — Electron edge only.
 *
 * Windows, permission prompts, global shortcuts, and IPC live here; call
 * epochs, protocol events, and socket ownership live in HostSession. The
 * renderer owns devices; the daemon never sees an Electron object.
 */
import { app, BrowserWindow, desktopCapturer, ipcMain, session } from "electron";
import path from "node:path";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { HostSession } from "../host/hostSession.js";
import { hostLog } from "./hostLog.js";
import {
  ElectronNotifications,
  ElectronPermissions,
  ElectronShortcuts,
} from "../host/electronMain.js";
import type { HostEvent } from "../host/protocol.js";
import type { AudioFrameHeader, ScreenFrameMetadata } from "../shared/protocol.js";
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
// The daemon's `ready` can arrive before the renderer has loaded (a warm
// runtime answers in milliseconds; the page takes longer). Keep the last one
// and whether the socket has closed since, so the renderer can ask instead of
// waiting for a control that already went by.
let lastReady: unknown = null;
let duplexClosed = false;
const permissions = new ElectronPermissions();
const shortcuts = new ElectronShortcuts();
const notifications = new ElectronNotifications();
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
    global_shortcuts: true,
    notifications: true,
  },
  onControl: (c) => {
    const type = (c as { type?: string })?.type;
    if (type === "ready") {
      lastReady = c;
      duplexClosed = false;
    }
    if (type && type !== "screen.frame.accepted") {
      hostLog("daemon", String(type));
    } else if (type === "screen.frame.accepted") {
      const f = c as { frame_id?: string; context_sampled?: boolean };
      hostLog("ingestion", `frame.accepted id=${f.frame_id} sampled=${f.context_sampled}`);
    }
    sendToRenderer("duplex:control", c);
  },
  onAudio: (pcm) => sendToRenderer("duplex:audio", pcm),
  onClosed: (code) => {
    duplexClosed = true;
    hostLog("transport", `duplex closed code=${code}`);
    sendToRenderer("duplex:closed", code, "");
  },
  onScreen: (update) => {
    if (update.channel) {
      const ch = update.channel;
      hostLog(
        "transport",
        `screen config token=${ch.token ? "issued" : "absent"} ` +
          `rate=${ch.recommended_frame_rate ?? "?"}Hz path=${ch.path ?? "?"}`,
      );
    }
    if (update.control) {
      const ctl = update.control as { type?: string };
      hostLog("ingestion", `screen ${String(ctl?.type ?? "?")}`);
    }
    sendToRenderer("screen:update", update);
  },
});

function createWindow(): void {
  // The product UI lays out a chat beside an agent panel; below ~1100 px the
  // two overlap, so the window opens at a comfortable desktop size.
  win = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1100,
    minHeight: 700,
    title: "GNSIS",
    webPreferences: {
      preload: path.join(__dirname, "preload.mjs"),
      contextIsolation: true,
      sandbox: false,
      nodeIntegration: false,
    },
  });
  // GNSIS_DEMO=1 fills the dock with the sample agents so every state of the
  // interface can be reviewed in the packaged app, where there is no URL to
  // add ?demo to.
  const query = process.env.GNSIS_DEMO ? { demo: "1" } : undefined;
  win.loadFile(path.join(__dirname, "../renderer/index.html"), query ? { query } : undefined);
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
  hostLog("host", `ready runtime=${RUNTIME_URL} session=${SESSION_ID} pid=${process.pid}`);
  // Screen perception: the renderer's getDisplayMedia() is answered here, or
  // Chromium refuses it. The whole primary display, so the visual sense sees
  // what the person sees; on macOS 15+ the system picker is offered instead,
  // and the OS asks for Screen Recording permission on first use either way.
  session.defaultSession.setDisplayMediaRequestHandler(
    async (_request, callback) => {
      try {
        const sources = await desktopCapturer.getSources({ types: ["screen"] });
        if (sources.length === 0) {
          hostLog("permissions", "screen: no display source available");
          callback({});
          return;
        }
        hostLog("permissions", `screen: capturing ${sources[0].name}`);
        callback({ video: sources[0] });
      } catch (err) {
        hostLog("permissions", `screen: source lookup failed: ${String(err)}`);
        callback({});
      }
    },
    { useSystemPicker: true },
  );
  createWindow();
  host.connect(SESSION_ID);
  host.ready();

  shortcuts.register("Control+Alt+Space", () => {
    host.interrupt("global_shortcut");
    sendToRenderer("ui:interrupted");
  });

  ipcMain.handle("media:permissions", async () => {
    const status = {
      microphone: await permissions.status("microphone"),
      camera: await permissions.status("camera"),
      screen: await permissions.status("screen"),
    };
    hostLog("permissions", `status ${JSON.stringify(status)}`);
    return status;
  });
  ipcMain.handle("media:request", async (_e, kind) => {
    const result = await permissions.request(kind);
    hostLog("permissions", `request ${String(kind)} -> ${String(result)}`);
    return result;
  });

  ipcMain.on("duplex:control", (_e, control) => {
    if ((control as { type?: string })?.type === "stop") host.endCall("client_stop");
    else host.emit(control as HostEvent);
  });
  ipcMain.on("host:event", (_e, event) => {
    const type = (event as HostEvent)?.type;
    if (typeof type === "string" && type.startsWith("playback.")) {
      hostLog("playback", type);
    }
    host.emit(event as HostEvent);
  });
  ipcMain.handle("link:state", () => ({
    ready: lastReady,
    connected: host.connected,
    closed: duplexClosed,
  }));
  ipcMain.on("session:reconnect", () => {
    // The renderer asks for this when the person starts live voice after the
    // daemon session ended or the connection dropped. Same session id: the
    // daemon rebinds within its grace window, otherwise starts a fresh one.
    hostLog("transport", "reconnect requested by renderer");
    lastReady = null;
    duplexClosed = false;
    host.connect(SESSION_ID);
    host.ready();
  });
  ipcMain.on("call:start", () => host.startCall());
  ipcMain.on("call:end", (_e, reason) => {
    const why = typeof reason === "string" && reason ? reason : "renderer";
    hostLog("host", `call ended reason=${why}`);
    host.endCall(why, { stop: false });
  });
  ipcMain.on("duplex:audioFrame", (_e, header: AudioFrameHeader, pcm: Uint8Array) =>
    host.sendAudioFrame(header, Buffer.from(pcm)),
  );
  ipcMain.on("screen:frame", (_e, metadata: ScreenFrameMetadata, payload: Uint8Array) =>
    host.sendScreenFrame(metadata, Buffer.from(payload)),
  );
  ipcMain.on("host:log", (_e, line) => {
    if (typeof line === "string") hostLog("renderer", line);
  });
  ipcMain.handle("tool:call", async (_e, tool: string, args: unknown) => {
    try {
      const res = await tools.call(tool, args as Record<string, unknown>);
      hostLog("execution", `tool ${String(tool)} ok`);
      return res;
    } catch (err) {
      hostLog("execution", `tool ${String(tool)} failed: ${String(err)}`);
      throw err;
    }
  });
});

app.on("will-quit", () => {
  hostLog("host", "will-quit");
  shortcuts.unregisterAll();
  host.disconnect("app_quit");
});

app.on("window-all-closed", () => {
  hostLog("host", "window-all-closed");
  if (process.platform !== "darwin") app.quit();
});

app.on("activate", () => {
  // macOS: closing the window keeps the app alive; a dock click must bring a
  // usable window back rather than leaving a windowless Host.
  hostLog("host", "activate");
  if (win === null) createWindow();
});

app.on("second-instance", () => {
  hostLog("host", "second-instance");
  if (win !== null) {
    if (win.isMinimized()) win.restore();
    win.focus();
  } else {
    createWindow();
  }
});
