/**
 * Persistent host log for real-device acceptance diagnosis.
 *
 * One line per event at <userData>/logs/gnsis-host.log, tagged by layer so a
 * failure can be attributed without a debugger: packaging/host lifecycle,
 * permissions, daemon transport, screen/audio ingestion, playback, and
 * execution. The daemon keeps its own server-side log; this file is the
 * Desktop Host half of the story.
 */
import { app } from "electron";
import { appendFileSync, mkdirSync } from "node:fs";
import path from "node:path";

let logPath: string | null = null;

export function hostLog(layer: string, message: string): void {
  try {
    if (!logPath) {
      const dir = path.join(app.getPath("userData"), "logs");
      mkdirSync(dir, { recursive: true });
      logPath = path.join(dir, "gnsis-host.log");
    }
    const ts = new Date().toISOString();
    appendFileSync(logPath, `${ts} [${layer}] ${message}\n`);
  } catch {
    // Logging must never break the app itself.
  }
}
