import { spawn } from "node:child_process";
import path from "node:path";

interface OutputLike {
  setEncoding(encoding: "utf8"): unknown;
  on(event: "data", listener: (chunk: unknown) => void): unknown;
}

interface ProcessLike {
  stdout: OutputLike;
  stderr: OutputLike;
  once(event: "close", listener: (code: number | null) => void): unknown;
  once(event: "error", listener: (error: Error) => void): unknown;
  kill(): boolean;
}

export interface TapGestureOptions {
  platform?: NodeJS.Platform;
  helperPath?: string;
  resourcesPath?: string;
  spawn?: (command: string, args: string[]) => ProcessLike;
  onGesture(kind: "single" | "double"): void;
  log(line: string): void;
  setTimeout?: (fn: () => void, ms: number) => unknown;
  clearTimeout?: (timer: unknown) => void;
}

export class TapGesture {
  private process: ProcessLike | null = null;
  private retry: unknown = null;
  private stopped = false;
  private unsupported = false;
  private buffer = "";
  private readonly setT: (fn: () => void, ms: number) => unknown;
  private readonly clearT: (timer: unknown) => void;

  constructor(private readonly options: TapGestureOptions) {
    this.setT = options.setTimeout ?? ((fn, ms) => setTimeout(fn, ms));
    this.clearT =
      options.clearTimeout ??
      ((timer) => clearTimeout(timer as ReturnType<typeof setTimeout>));
  }

  start(): void {
    this.stopped = false;
    if (
      this.process ||
      this.unsupported ||
      (this.options.platform ?? process.platform) !== "darwin"
    ) return;
    const helper =
      this.options.helperPath ??
      path.join(
        this.options.resourcesPath ?? process.resourcesPath,
        "tap",
        "gnsis-tap-listener",
      );
    try {
      const child = (
        this.options.spawn ??
        ((command, args) =>
          spawn(command, args, {
            stdio: ["ignore", "pipe", "pipe"],
          }) as unknown as ProcessLike)
      )(helper, []);
      this.process = child;
      child.stdout.setEncoding("utf8");
      child.stdout.on("data", (chunk) => this.consume(String(chunk)));
      child.stderr.setEncoding("utf8");
      child.stderr.on("data", (chunk) => {
        const line = String(chunk).trim();
        if (line) this.options.log(`tap gesture: helper ${line}`);
      });
      child.once("error", (error) => {
        this.unsupported = true;
        this.options.log(`tap gesture: helper failed (${String(error)})`);
      });
      child.once("close", (code) => {
        if (this.process !== child) return;
        this.process = null;
        if (this.stopped || this.unsupported) return;
        this.options.log(`tap gesture: helper exited (${String(code)}); retrying`);
        this.retry = this.setT(() => {
          this.retry = null;
          this.start();
        }, 5_000);
      });
    } catch (error) {
      this.options.log(`tap gesture: unavailable (${String(error)})`);
    }
  }

  stop(): void {
    this.stopped = true;
    this.unsupported = false;
    if (this.retry !== null) this.clearT(this.retry);
    this.retry = null;
    this.process?.kill();
    this.process = null;
  }

  private consume(chunk: string): void {
    this.buffer += chunk;
    for (;;) {
      const newline = this.buffer.indexOf("\n");
      if (newline < 0) return;
      const line = this.buffer.slice(0, newline).trim();
      this.buffer = this.buffer.slice(newline + 1);
      if (line) this.message(line);
    }
  }

  private message(line: string): void {
    let message: unknown;
    try {
      message = JSON.parse(line);
    } catch {
      this.options.log("tap gesture: ignored malformed helper output");
      return;
    }
    if (!message || typeof message !== "object") return;
    const value = message as { status?: unknown; gesture?: unknown; message?: unknown };
    if (value.status === "ready") {
      this.options.log("tap gesture: listening for a deliberate double tap");
    } else if (value.status === "unsupported") {
      this.unsupported = true;
      this.options.log("tap gesture: this Mac has no supported motion sensor");
    } else if (value.status === "error") {
      this.unsupported = true;
      this.options.log(
        `tap gesture: ${typeof value.message === "string" ? value.message : "sensor failed"}`,
      );
    } else if (value.gesture === "single" || value.gesture === "double") {
      const kind = value.gesture;
      this.options.log(`tap gesture: ${kind} tap detected`);
      this.options.onGesture(kind);
    }
  }
}
