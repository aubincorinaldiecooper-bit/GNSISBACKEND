import { WebSocket, WebSocketServer } from "ws";

export interface BrowserTaskResult {
  success: boolean;
  data: string;
}

type Pending = {
  resolve: (result: BrowserTaskResult) => void;
  reject: (error: Error) => void;
  timer: NodeJS.Timeout;
};

/**
 * Local bridge between GNSIS Desktop and the GNSIS Browser extension hub.
 *
 * The extension is the WebSocket client. The desktop owns the loopback
 * server because the realtime runtime may be remote while browser control
 * must remain on the person's machine/profile.
 */
export class BrowserHubServer {
  private server: WebSocketServer | null = null;
  private client: WebSocket | null = null;
  private ready = false;
  private pending: Pending | null = null;
  private actualPort = 0;

  constructor(private readonly requestedPort = 8790) {}

  get port(): number {
    return this.actualPort || this.requestedPort;
  }

  get connected(): boolean {
    return this.ready && this.client?.readyState === WebSocket.OPEN;
  }

  get extensionUrl(): string {
    return `chrome-extension://akldabonmimlicnjlflnapfeklbfemhj/hub.html?ws=${this.port}`;
  }

  async start(): Promise<number> {
    if (this.server) return this.port;

    const server = new WebSocketServer({ host: "127.0.0.1", port: this.requestedPort });
    this.server = server;

    await new Promise<void>((resolve, reject) => {
      const onListening = () => {
        server.off("error", onError);
        resolve();
      };
      const onError = (err: Error) => {
        server.off("listening", onListening);
        reject(err);
      };
      server.once("listening", onListening);
      server.once("error", onError);
    });

    const address = server.address();
    this.actualPort = typeof address === "object" && address ? address.port : this.requestedPort;

    server.on("connection", (socket) => {
      if (this.client && this.client.readyState === WebSocket.OPEN) {
        this.client.close(1000, "replaced");
      }
      this.client = socket;
      this.ready = false;

      socket.on("message", (raw) => this.onMessage(String(raw)));
      socket.on("close", () => {
        if (this.client === socket) {
          this.client = null;
          this.ready = false;
        }
        this.failPending(new Error("GNSIS Browser extension disconnected."));
      });
      socket.on("error", () => {
        this.failPending(new Error("GNSIS Browser extension connection failed."));
      });
    });

    return this.port;
  }

  async waitUntilReady(timeoutMs = 15_000): Promise<void> {
    const deadline = Date.now() + timeoutMs;
    while (!this.connected && Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    if (!this.connected) {
      throw new Error(
        "GNSIS Browser extension did not connect. Install/enable it, then open its hub page and try again.",
      );
    }
  }

  async execute(task: string, timeoutMs = 180_000): Promise<BrowserTaskResult> {
    const clean = task.trim();
    if (!clean) throw new Error("Browser task is empty.");
    if (!this.connected || !this.client) {
      throw new Error("GNSIS Browser extension is not connected.");
    }
    if (this.pending) {
      throw new Error("A browser task is already running.");
    }

    return new Promise<BrowserTaskResult>((resolve, reject) => {
      const timer = setTimeout(() => {
        try {
          this.client?.send(JSON.stringify({ type: "stop" }));
        } catch {
          // The timeout result is already definitive for this desktop call.
        }
        this.pending = null;
        reject(new Error("Browser task timed out."));
      }, timeoutMs);

      this.pending = { resolve, reject, timer };
      this.client!.send(JSON.stringify({ type: "execute", task: clean }));
    });
  }

  async close(): Promise<void> {
    this.failPending(new Error("Browser hub closed."));
    const client = this.client;
    this.client = null;
    this.ready = false;
    try {
      client?.close(1000, "desktop closing");
    } catch {
      // ignore shutdown races
    }

    const server = this.server;
    this.server = null;
    if (!server) return;
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }

  private onMessage(raw: string): void {
    let message: Record<string, unknown>;
    try {
      message = JSON.parse(raw) as Record<string, unknown>;
    } catch {
      return;
    }

    switch (message.type) {
      case "ready":
        this.ready = true;
        return;
      case "result": {
        const pending = this.pending;
        if (!pending) return;
        this.pending = null;
        clearTimeout(pending.timer);
        pending.resolve({
          success: message.success === true,
          data: typeof message.data === "string" ? message.data : "",
        });
        return;
      }
      case "error": {
        const pending = this.pending;
        if (!pending) return;
        this.pending = null;
        clearTimeout(pending.timer);
        pending.reject(
          new Error(typeof message.message === "string" ? message.message : "Browser task failed."),
        );
        return;
      }
    }
  }

  private failPending(error: Error): void {
    const pending = this.pending;
    if (!pending) return;
    this.pending = null;
    clearTimeout(pending.timer);
    pending.reject(error);
  }
}
