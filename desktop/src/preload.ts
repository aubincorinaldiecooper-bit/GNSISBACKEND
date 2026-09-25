/**
 * Preload bridge — the renderer's only window into the host.
 * Context isolation stays on; everything crosses as IPC data.
 */
import { contextBridge, ipcRenderer } from "electron";

contextBridge.exposeInMainWorld("gnsis", {
  mediaPermissions: () => ipcRenderer.invoke("media:permissions"),
  requestPermission: (kind: string) => ipcRenderer.invoke("media:request", kind),
  captureScreenshot: () => ipcRenderer.invoke("screenshot:capture"),
  sendControl: (control: unknown) => ipcRenderer.send("duplex:control", control),
  sendHostEvent: (event: unknown) => ipcRenderer.send("host:event", event),
  startCall: () => ipcRenderer.send("call:start"),
  sendAudioFrame: (header: unknown, pcm: Uint8Array) =>
    ipcRenderer.send("duplex:audioFrame", header, pcm),
  sendScreenFrame: (metadata: unknown, payload: Uint8Array) =>
    ipcRenderer.send("screen:frame", metadata, payload),
  callTool: (tool: string, args: Record<string, unknown>) =>
    ipcRenderer.invoke("tool:call", tool, args),
  onControl: (fn: (control: unknown) => void) =>
    ipcRenderer.on("duplex:control", (_e, c) => fn(c)),
  onAudio: (fn: (pcm: Uint8Array) => void) =>
    ipcRenderer.on("duplex:audio", (_e, pcm) => fn(pcm)),
  onClosed: (fn: (code: number, reason: string) => void) =>
    ipcRenderer.on("duplex:closed", (_e, code, reason) => fn(code, reason)),
  onInterrupted: (fn: () => void) => ipcRenderer.on("ui:interrupted", fn),
});
