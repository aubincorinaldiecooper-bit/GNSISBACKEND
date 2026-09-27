/**
 * Running the system's own tools (open, osascript, mdfind, lsappinfo).
 *
 * Always execFile with an argument list — never a shell — so a file name or
 * an app name the model produced can never become a command. The adapters
 * take a `Shell`, so tests can stand in for macOS on any machine.
 */
import { execFile } from "node:child_process";

export interface ShellResult {
  code: number;
  stdout: string;
  stderr: string;
}

export interface Shell {
  run(file: string, args: string[], opts?: { timeoutMs?: number }): Promise<ShellResult>;
}

export const systemShell: Shell = {
  run(file, args, opts = {}) {
    return new Promise((resolve) => {
      execFile(
        file,
        args,
        { timeout: opts.timeoutMs ?? 15_000, maxBuffer: 4 * 1024 * 1024, encoding: "utf8" },
        (error, stdout, stderr) => {
          const code =
            error == null ? 0 : typeof (error as { code?: unknown }).code === "number" ? (error as { code: number }).code : 1;
          resolve({ code, stdout: String(stdout ?? ""), stderr: String(stderr ?? "") || (error ? String(error.message) : "") });
        },
      );
    });
  },
};

/** Why an AppleScript/JXA run failed, from the error number macOS reports. */
export type ScriptFailure =
  | { kind: "automation_denied"; app: string } //  -1743: not allowed to control that app
  | { kind: "accessibility_denied" } //            -1719 / -25211 / "assistive access"
  | { kind: "not_running"; app: string } //        -600
  | { kind: "no_window" } //                       -1728 / -1719 on a missing window
  | { kind: "other"; message: string };

export function classifyScriptError(stderr: string, app: string): ScriptFailure {
  const text = stderr || "";
  if (/-1743\b/.test(text) || /not authori[sz]ed to send apple events/i.test(text)) {
    return { kind: "automation_denied", app };
  }
  if (/-25211\b/.test(text) || /assistive access/i.test(text) || /not allowed to send keystrokes/i.test(text)) {
    return { kind: "accessibility_denied" };
  }
  if (/-600\b/.test(text)) return { kind: "not_running", app };
  if (/-1728\b/.test(text) || /-1719\b/.test(text)) return { kind: "no_window" };
  return { kind: "other", message: text.trim().split("\n").pop()?.slice(0, 200) ?? "" };
}

/** Run a JXA script; returns stdout, or throws the classified failure. */
export async function jxa(shell: Shell, script: string, app: string, timeoutMs = 15_000): Promise<string> {
  const result = await shell.run("/usr/bin/osascript", ["-l", "JavaScript", "-e", script], { timeoutMs });
  if (result.code !== 0) throw classifyScriptError(result.stderr, app);
  return result.stdout.trim();
}

/** Run an AppleScript; returns stdout, or throws the classified failure. */
export async function applescript(shell: Shell, script: string, app: string, timeoutMs = 15_000): Promise<string> {
  const result = await shell.run("/usr/bin/osascript", ["-e", script], { timeoutMs });
  if (result.code !== 0) throw classifyScriptError(result.stderr, app);
  return result.stdout.trim();
}

/** A string literal for an AppleScript or JXA source: JSON quoting works for both. */
export function literal(value: string): string {
  return JSON.stringify(value);
}
