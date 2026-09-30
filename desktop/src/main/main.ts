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
  nativeImage,
  screen as displays,
  session,
  systemPreferences,
  Tray,
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
import { ClickThrough, hitRectsFrom } from "./clickThrough.js";
import { MenuBar } from "./menuBar.js";
import { applyWindowRules } from "./windowRules.js";
import { PersonApp, appToRestore } from "./personApp.js";

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
  aroundInput: asTheirInput,
});

/** The app the person was last using, noted each time an app comes to the front (macOS). */
const personApp = new PersonApp(app.getName(), frontAppName);

/**
 * Keys, typing and clicks GNSIS sends go to the app the person is using, not
 * to GNSIS. When GNSIS is in front (the person just clicked one of its cards,
 * or answered its Allow box), their app is put back in front first. While the
 * action runs, the floating window lets every click through, so a click GNSIS
 * sends reaches the app underneath even where a card is.
 */
async function asTheirInput<T>(run: () => Promise<T>): Promise<T> {
  clickThrough?.suspend();
  try {
    const back = appToRestore(await frontAppName(), app.getName(), personApp.name);
    if (back) {
      // Activating a running app needs no permission.
      await systemShell.run("/usr/bin/open", ["-a", back]);
      await new Promise((resolve) => setTimeout(resolve, 300));
      hostLog("execution", "put the person's app back in front before typing or clicking");
    }
    return await run();
  } finally {
    clickThrough?.resume();
  }
}

/**
 * Ask the person before an action runs. A native alert that GNSIS comes
 * forward for, so it is seen whatever app the person is in; an action that
 * types or clicks then puts their app back in front before it runs
 * (asTheirInput). Floating, it is a free-standing alert in the middle of the
 * screen: a sheet would hang from the top edge of the see-through window,
 * where nothing else of GNSIS is.
 */
async function askPerson(request: ConfirmRequest, signal: AbortSignal): Promise<boolean> {
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
  const result = win && !OVERLAY ? await dialog.showMessageBox(win, options) : await dialog.showMessageBox(options);
  const allowed = result.response === 0 && !signal.aborted;
  hostLog("execution", `call ${request.callId}: the person ${allowed ? "allowed" : "did not allow"} it`);
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

/**
 * How GNSIS appears. On macOS it floats: one see-through window over the
 * main display's usable area (all but the menu bar and the Dock), kept above
 * other windows, where only GNSIS's own cards take clicks and everything
 * else falls through to the apps underneath. There is no backdrop of its own.
 * GNSIS_WINDOW=standard puts it back in an ordinary window with a drawn
 * backdrop. Other systems always get the ordinary window: on Linux the
 * pointer stops being reported once a window lets clicks through, so GNSIS
 * would stop taking clicks for good.
 */
const WINDOW_ASKED = process.env.GNSIS_WINDOW ?? (process.platform === "darwin" ? "overlay" : "standard");
const OVERLAY = WINDOW_ASKED === "overlay" && process.platform === "darwin";
let clickThrough: ClickThrough | null = null;
/**
 * How large GNSIS draws itself; 1 is the size it was designed at. 0.8 is the
 * owner's pick after comparing 100%, 90% and 80% on a 13-inch screen: close
 * to the text size of ordinary Mac apps, and the chat is centred there.
 * GNSIS_SCALE (0.6 to 1.25) tries another size without a rebuild.
 */
const UI_SCALE = (() => {
  const asked = Number(process.env.GNSIS_SCALE);
  return Number.isFinite(asked) && asked >= 0.6 && asked <= 1.25 ? asked : 0.8;
})();

/**
 * GNSIS's icon in the Mac menu bar, for tucking the floating window away and
 * bringing it back (see menuBar.ts). Only where GNSIS floats: in an ordinary
 * window, the window's own buttons do this.
 */
const menuBar = OVERLAY
  ? new MenuBar({
      makeTray: (image) => new Tray(image as Electron.NativeImage),
      image: (png) => {
        const image = nativeImage.createEmpty();
        image.addRepresentation({ scaleFactor: 2, dataURL: png });
        image.setTemplateImage(true);
        return image;
      },
      window: () => win,
      send: (message) => sendToRenderer("menubar", message),
      zoom: UI_SCALE,
      log: (line) => hostLog("host", line),
      reopen: () => createWindow(),
      endCall: () => {
        if (!callOpen) return;
        callOpen = false;
        hostLog("host", "call ended reason=tucked_away (the page did not end it)");
        host.endCall("tucked_away", { stop: false });
      },
    })
  : null;
/** A call is open on the timeline (the page's call:start without its call:end yet). */
let callOpen = false;
/** Microphone frames that arrived while GNSIS was tucked into the menu bar, and were not sent. */
let tuckedFrames = 0;

function createWindow(): void {
  const webPreferences = {
    preload: path.join(__dirname, "preload.mjs"),
    contextIsolation: true,
    sandbox: false,
    nodeIntegration: false,
    zoomFactor: UI_SCALE,
    // The page learns from its preload whether it floats, before it first draws.
    additionalArguments: [`--gnsis-overlay=${OVERLAY ? "1" : "0"}`, `--gnsis-menubar=${menuBar ? "1" : "0"}`],
  };
  if (OVERLAY) {
    win = new BrowserWindow({
      ...displays.getPrimaryDisplay().workArea,
      title: "GNSIS",
      transparent: true,
      backgroundColor: "#00000000",
      frame: false,
      hasShadow: false,
      resizable: false,
      movable: false,
      maximizable: false,
      fullscreenable: false,
      // A click on a card works the first time, even while another app is in
      // front (which, floating, is nearly always).
      acceptFirstMouse: true,
      webPreferences,
    });
    // Above ordinary windows, so clicking an app underneath does not bury GNSIS.
    win.setAlwaysOnTop(true, "floating");
    clickThrough = new ClickThrough(win, () => displays.getCursorScreenPoint());
    clickThrough.start();
  } else {
    // The product UI keeps room for the Activity drawer beside the chat; at
    // 1100 px the chat narrows a little to keep that room, so that is the floor.
    win = new BrowserWindow({
      width: 1440,
      height: 900,
      minWidth: 1100,
      minHeight: 700,
      title: "GNSIS",
      webPreferences,
    });
  }
  const ruled = applyWindowRules(win, OVERLAY);
  if (WINDOW_ASKED === "overlay" && !OVERLAY) hostLog("host", `window: floating is macOS only; ordinary window on ${process.platform}`);
  hostLog("host", `window: ${OVERLAY ? "floating over the desktop" : "ordinary window"} at ${Math.round(UI_SCALE * 100)}%; ${ruled.join("; ")}`);
  // GNSIS_DEMO=1 fills the dock with the sample agents so every state of the
  // interface can be reviewed in the packaged app, where there is no URL to
  // add ?demo to.
  const query = process.env.GNSIS_DEMO ? { demo: "1" } : undefined;
  win.loadFile(path.join(__dirname, "../renderer/index.html"), query ? { query } : undefined);
  win.on("closed", () => {
    clickThrough?.stop();
    clickThrough = null;
    win = null;
  });
}

/** The floating window follows the screen it covers when displays change. */
function fitOverlay(): void {
  if (!OVERLAY || !win || win.isDestroyed()) return;
  win.setBounds(displays.getPrimaryDisplay().workArea);
  // The menu bar icon is where GNSIS tucks into; tell the page where it now is.
  const at = menuBar?.iconAt();
  if (at) sendToRenderer("menubar", { at });
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
  // GNSIS takes no screenshots: listing the screens asks for no thumbnails,
  // which Electron would otherwise make of each one (desktop/AGENTS.md).
  session.defaultSession.setDisplayMediaRequestHandler(
    async (_request, callback) => {
      try {
        const sources = await desktopCapturer.getSources({ types: ["screen"], thumbnailSize: { width: 0, height: 0 } });
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
    callOpen = true;
    callFrames = 0;
    hostLog("host", "call started");
    host.startCall();
  });
  ipcMain.on("call:end", (_e, reason) => {
    callOpen = false;
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
    // Tucked into the menu bar, GNSIS is not listening: nothing from the microphone leaves this Mac.
    if (menuBar?.tucked) {
      if (tuckedFrames++ % 500 === 0) hostLog("audio", `GNSIS is tucked into the menu bar; microphone frames are not sent (${tuckedFrames} so far)`);
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
  // A yes/no question from the page, such as "Erase your GNSIS?", asked the
  // same way as the Allow box: free-standing when GNSIS floats.
  ipcMain.handle("dialog:confirm", async (_e, message: unknown, confirmLabel: unknown) => {
    if (typeof message !== "string" || !message.trim() || message.length > 300) return false;
    const yes = typeof confirmLabel === "string" && confirmLabel.trim() && confirmLabel.length <= 40 ? confirmLabel : "OK";
    const options = { type: "warning" as const, buttons: [yes, "Cancel"], defaultId: 1, cancelId: 1, noLink: true, title: "GNSIS", message };
    const result = win && !OVERLAY ? await dialog.showMessageBox(win, options) : await dialog.showMessageBox(options);
    return result.response === 0;
  });
  // Where the floating window's cards are, so clicks elsewhere fall through.
  let reported = false;
  ipcMain.on("hit:rects", (_e, rects: unknown) => {
    if (!clickThrough) return;
    clickThrough.setRects(rects);
    // Once: proof the page is telling main where its cards are. Without it,
    // every click would fall through and GNSIS could not be used.
    const cards = hitRectsFrom(rects).length;
    if (reported || cards === 0) return;
    reported = true;
    hostLog("host", `floating window: the page reported ${cards} card(s) that take clicks`);
  });
  if (process.platform === "darwin") {
    // Each time an app comes to the front, note it if it is not GNSIS.
    systemPreferences.subscribeWorkspaceNotification("NSWorkspaceDidActivateApplicationNotification", () => void personApp.noteFront());
    void personApp.noteFront();
  }
  displays.on("display-metrics-changed", fitOverlay);
  displays.on("display-added", fitOverlay);
  displays.on("display-removed", fitOverlay);
  // The menu bar icon: the page's drawing of the face, and "tucked away, hide the window now".
  ipcMain.on("menubar:face", (_e, png: unknown) => menuBar?.setFace(png));
  ipcMain.on("menubar:hide", () => menuBar?.pageTuckedAway());
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
  menuBar?.destroy();
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
  // Tucked into the menu bar: the Dock icon brings GNSIS back too.
  else if (menuBar?.tucked) menuBar.show();
});

app.on("second-instance", () => {
  hostLog("host", "second-instance");
  if (win !== null) {
    if (menuBar?.tucked) menuBar.show();
    if (win.isMinimized()) win.restore();
    win.focus();
  } else {
    createWindow();
  }
});
