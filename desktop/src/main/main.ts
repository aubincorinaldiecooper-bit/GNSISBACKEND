/**
 * GNSIS desktop main process — Electron edge only.
 *
 * Windows, permission prompts, global shortcuts, and IPC live here; call
 * epochs, protocol events, and socket ownership live in HostSession. The
 * renderer owns devices; the daemon never sees an Electron object.
 */
import {
  app,
  BrowserWindow,
  desktopCapturer,
  dialog,
  ipcMain,
  screen as displays,
  session,
  systemPreferences,
} from "electron";
import os from "node:os";
import path from "node:path";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { HostSession } from "../host/hostSession.js";
import { ActionBroker, type ConfirmRequest } from "../host/actionBroker.js";
import { reasonForLog } from "../host/actionPolicy.js";
import { TurnLog } from "../host/turns.js";
import { runtimeTranscriber, UtteranceTranscriber } from "../host/utterances.js";
import { TypedTurns } from "../host/typedTurns.js";
import { ScreenWatch } from "../host/screenWatch.js";
import { actionsAllowed } from "../host/runtimeTrust.js";
import { hostLog } from "./hostLog.js";
import {
  ElectronNotifications,
  ElectronPermissions,
  ElectronShortcuts,
} from "../host/electronMain.js";
import type { HostEvent } from "../host/protocol.js";
import { audioFrameHeader, type ClientControl, type ScreenFrameMetadata } from "../shared/protocol.js";
import { ToolRegistry } from "../tools/registry.js";
import { HOST_TOOL_SCHEMAS, HOST_TOOLS_VERSION } from "../tools/catalog.js";
import { FilesTool } from "../tools/files.js";
import { MacFinder } from "../tools/mac/finder.js";
import { OpenTool } from "../tools/mac/open.js";
import { BrowserTool } from "../tools/mac/browser.js";
import { InputTool } from "../tools/mac/input.js";
import { systemShell } from "../tools/mac/shell.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// Runtime resolution: GNSIS_RUNTIME_URL env var first (dev/CI), then a
// persisted desktop setting at <userData>/gnsis.json ({"runtimeUrl": ...}),
// then the public GNSIS service. Normal users never configure this: a fresh
// install connects to https://gnsis.studio automatically. The env/JSON seams
// remain for local development and future LocalGNSISProvider work.
function readSettings(): { runtimeUrl?: unknown; actions?: unknown } {
  try {
    const cfgPath = path.join(app.getPath("userData"), "gnsis.json");
    return JSON.parse(readFileSync(cfgPath, "utf8")) as { runtimeUrl?: unknown; actions?: unknown };
  } catch {
    // no config file / unreadable — defaults apply
    return {};
  }
}
const SETTINGS = readSettings();

function resolveRuntimeUrl(): string {
  const env = process.env.GNSIS_RUNTIME_URL;
  if (env) return env;
  if (typeof SETTINGS.runtimeUrl === "string" && SETTINGS.runtimeUrl) {
    return SETTINGS.runtimeUrl;
  }
  return "https://gnsis.studio";
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

// The actions GNSIS can take on this machine when the model asks. Offered to
// the runtime on connect, so the model is only ever told about what this
// machine can actually do — and only to a runtime this desktop trusts with
// them (host/runtimeTrust.ts).
const finder = process.platform === "darwin" ? new MacFinder(systemShell) : undefined;
const filesTool = new FilesTool({ home: os.homedir(), finder });
const inputTool = new InputTool(systemShell, {
  bounds: () => displays.getPrimaryDisplay().bounds,
});
tools.registerAction(new OpenTool(systemShell, filesTool));
tools.registerAction(filesTool);
tools.registerAction(new BrowserTool(systemShell, (combo) => inputTool.press(combo)));
tools.registerAction(inputTool);
const ACTIONS_TRUST = actionsAllowed(
  RUNTIME_URL,
  process.env.GNSIS_ACTIONS === "on" || process.env.GNSIS_ACTIONS === "off"
    ? process.env.GNSIS_ACTIONS
    : SETTINGS.actions === true
      ? "on"
      : SETTINGS.actions === false
        ? "off"
        : undefined,
);
const OFFERED_ACTIONS = ACTIONS_TRUST.allowed
  ? tools.actionNames(process.platform, HOST_TOOL_SCHEMAS.map((tool) => tool.name))
  : [];
const turns = new TurnLog();
const screenWatch = new ScreenWatch();
// The person's own words, so an action they asked for by name runs without a
// second ask. Only where actions are offered; see host/utterances.ts.
const utterances =
  OFFERED_ACTIONS.length > 0
    ? new UtteranceTranscriber({
        transcribe: runtimeTranscriber(RUNTIME_URL),
        send: (control) => host.sendControl(control as unknown as ClientControl),
        turns,
        log: hostLog,
      })
    : null;
// What the person types, sent as their own turn on this connection and kept
// in the same TurnLog once the runtime accepts it. See host/typedTurns.ts.
const typedTurns = new TypedTurns({
  send: (control) => host.sendControl(control as unknown as ClientControl),
  turns,
  ready: () => host.connected && lastReady !== null && !duplexClosed,
  log: hostLog,
});

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
  hostTools: { names: OFFERED_ACTIONS, version: HOST_TOOLS_VERSION },
  onControl: (c) => {
    broker.handleControl(c as Record<string, unknown>);
    utterances?.handleControl(c as Record<string, unknown>);
    typedTurns.handleControl(c as Record<string, unknown>);
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
  onOpen: () => hostLog("transport", "runtime connected (duplex open)"),
  onAudio: (pcm) => sendToRenderer("duplex:audio", pcm),
  onClosed: (code) => {
    duplexClosed = true;
    hostLog("transport", `duplex closed code=${code}`);
    typedTurns.abandonAll();
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

const broker = new ActionBroker({
  registry: tools,
  send: (control) => host.sendControl(control as unknown as ClientControl),
  event: (event) => host.emit(event),
  confirm: askPerson,
  accessibility: (prompt) =>
    process.platform === "darwin" ? systemPreferences.isTrustedAccessibilityClient(prompt) : true,
  latestTurn: () => turns.latest(),
  waitForWords: () => utterances?.settled() ?? Promise.resolve(),
  log: hostLog,
  notify: (update) => sendToRenderer("action:update", update),
  lookAfter: (sinceMs) => screenWatch.lookAfter(sinceMs),
});

/**
 * Ask the person before an action runs. A native alert, because the GNSIS
 * window may be behind the app GNSIS is about to act on; GNSIS comes forward
 * for it, and when the action types or clicks into another app, that app is
 * put back in front before it runs.
 */
async function askPerson(request: ConfirmRequest, signal: AbortSignal): Promise<boolean> {
  const previous = request.typesIntoFrontApp ? await frontAppName() : null;
  // The summary can quote what is typed, or name a file or site: it is shown
  // to the person, not written to the log; the broker has logged the call.
  hostLog("execution", `call ${request.callId}: asking the person (${reasonForLog(request.reason)})`);
  app.focus({ steal: true });
  const options = {
    type: "question" as const,
    buttons: ["Allow", "Don’t Allow"],
    defaultId: 1,
    cancelId: 1,
    noLink: true,
    title: "GNSIS",
    message: `Allow GNSIS to ${lowerFirst(request.summary)}?`,
    detail: request.why,
    signal,
  };
  const result = win ? await dialog.showMessageBox(win, options) : await dialog.showMessageBox(options);
  const allowed = result.response === 0 && !signal.aborted;
  hostLog("execution", `call ${request.callId}: the person ${allowed ? "allowed" : "did not allow"} it`);
  if (allowed && previous && previous !== app.getName()) {
    // Activating a running app needs no permission; the action then types
    // into the app the person was using, not into GNSIS.
    await systemShell.run("/usr/bin/open", ["-a", previous]);
    await new Promise((resolve) => setTimeout(resolve, 300));
  }
  return allowed;
}

async function frontAppName(): Promise<string | null> {
  if (process.platform !== "darwin") return null;
  const front = await systemShell.run("/usr/bin/lsappinfo", ["front"], { timeoutMs: 3_000 });
  if (front.code !== 0 || !front.stdout.trim()) return null;
  const info = await systemShell.run("/usr/bin/lsappinfo", ["info", "-only", "name", front.stdout.trim()], { timeoutMs: 3_000 });
  return /"(?:LSDisplayName|name)"\s*=\s*"([^"]+)"/i.exec(info.stdout)?.[1] ?? null;
}

function lowerFirst(text: string): string {
  return text ? text[0].toLowerCase() + text.slice(1) : text;
}

function createWindow(): void {
  // The product UI keeps room for the Activity drawer beside the chat; at
  // 1100 px the chat narrows a little to keep that room, so that is the floor.
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
  // Browser control is native on macOS: the existing browser tool owns tabs
  // and navigation, while the input tool clicks/types into the front browser.
  // Screen perception already streams through the canonical screen.frame path,
  // so no Chrome extension or localhost browser hub is part of launch.
  hostLog("browser", "native browser control enabled; no extension required");

  hostLog("host", `ready runtime=${RUNTIME_URL} session=${SESSION_ID} pid=${process.pid}`);
  hostLog(
    "execution",
    `actions offered: ${OFFERED_ACTIONS.join(",") || "none"} (catalog ${HOST_TOOLS_VERSION}; ${ACTIONS_TRUST.why}); ` +
      `accessibility ${process.platform === "darwin" ? (systemPreferences.isTrustedAccessibilityClient(false) ? "granted" : "not granted") : "n/a"}`,
  );
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
  ipcMain.handle("turn:text", (_e, text: unknown) => typedTurns.send(text));
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
  // How much microphone audio a call really sent: the first frame, and the
  // total when it ends, so "the mic was on but nothing arrived" shows in the log.
  let callFrames = 0;
  let droppedFrames = 0;
  ipcMain.on("call:start", () => {
    callFrames = 0;
    hostLog("host", "call started");
    host.startCall();
  });
  ipcMain.on("call:end", (_e, reason) => {
    const why = typeof reason === "string" && reason ? reason : "renderer";
    hostLog("host", `call ended reason=${why} audio_frames_sent=${callFrames}`);
    host.endCall(why, { stop: false });
  });
  ipcMain.on("duplex:audioFrame", (_e, raw: unknown, pcm: unknown) => {
    const header = audioFrameHeader(raw);
    if (!header || !(pcm instanceof Uint8Array)) {
      // Once, then every 500th: a steady stream of bad frames would bury the log.
      if (droppedFrames++ % 500 === 0) hostLog("audio", `a microphone frame was not well formed; dropped (${droppedFrames} so far)`);
      return;
    }
    const audio = Buffer.from(pcm);
    host.sendAudioFrame(header, audio);
    callFrames += 1;
    if (callFrames === 1) hostLog("audio", `first microphone frame sent (${audio.byteLength} bytes)`);
    utterances?.feed(header, audio);
  });
  if (utterances) setInterval(() => utterances.idle(Date.now()), 1_000);
  ipcMain.on("screen:frame", (_e, metadata: ScreenFrameMetadata, payload: Uint8Array) => {
    if (host.sendScreenFrame(metadata, Buffer.from(payload))) {
      screenWatch.noteFrame(metadata.captured_at_ms, metadata.video_source);
    }
  });
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
