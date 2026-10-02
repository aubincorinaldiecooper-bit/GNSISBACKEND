import {
  FrameStream,
  VisualClient,
  type VisualSession,
} from "../../../sdks/typescript/src/index.js";
import type { ScreenFrameMetadata } from "../shared/protocol.js";

export const DESKTOP_VISUAL_ACTIONS = [
  "click",
  "type",
  "navigate",
  "back",
  "wait",
  "done",
] as const;

type DesktopVisualAction = (typeof DESKTOP_VISUAL_ACTIONS)[number];
type JsonObject = Record<string, unknown>;

interface VisualApi {
  createSession(): Promise<VisualSession>;
  closeSession(sessionId: string): Promise<JsonObject>;
  setTask(
    sessionId: string,
    goal: string,
    allowedActions: readonly string[],
  ): Promise<JsonObject>;
  decide(sessionId: string): Promise<JsonObject>;
  recordAttempt(sessionId: string, decisionId: string): Promise<JsonObject>;
}

interface VisualFrameStream {
  sendFrame(options: {
    frameId: string;
    capturedAtMs: number;
    image: Uint8Array;
    encoding: string;
    videoSource: string;
    metadata: JsonObject;
  }): Promise<JsonObject>;
  close(): Promise<void>;
}

interface VisualServices {
  host: VisualApi;
  planner(session: VisualSession): VisualApi;
  stream(session: VisualSession): Promise<VisualFrameStream>;
}

export interface DesktopVisualConnectorOptions {
  baseUrl: string;
  hostToken: string;
  task: string;
  capabilityManifestId: string;
  latestTurnId(): string | null;
  dispatch(control: Record<string, unknown>): boolean;
  log(area: string, line: string): void;
  services?: VisualServices;
}

interface PendingFrame {
  metadata: ScreenFrameMetadata;
  payload: Buffer;
}

interface PendingAction {
  resolve(response: JsonObject): void;
  reject(error: Error): void;
}

export class DesktopVisualConnector {
  private readonly services: VisualServices;
  private readonly pendingActions = new Map<string, PendingAction>();
  private readonly ownedCalls = new Set<string>();
  private session: VisualSession | null = null;
  private host: VisualApi | null = null;
  private planner: VisualApi | null = null;
  private stream: VisualFrameStream | null = null;
  private latestFrame: PendingFrame | null = null;
  private processing = false;
  private stopped = false;

  constructor(private readonly opts: DesktopVisualConnectorOptions) {
    const task = opts.task.trim();
    if (!task) throw new Error("desktop visual task must not be empty");
    if (!opts.capabilityManifestId.trim()) {
      throw new Error("desktop capability manifest id must not be empty");
    }
    this.services = opts.services ?? defaultServices(opts.baseUrl, opts.hostToken);
  }

  async start(): Promise<void> {
    if (this.session) return;
    try {
      this.host = this.services.host;
      this.session = await this.host.createSession();
      this.planner = this.services.planner(this.session);
      this.stream = await this.services.stream(this.session);
      await this.planner.setTask(
        this.session.sessionId,
        this.opts.task.trim(),
        DESKTOP_VISUAL_ACTIONS,
      );
      this.opts.log("visual", `desktop visual session ${this.session.sessionId} started`);
    } catch (error) {
      await this.close();
      throw error;
    }
  }

  observeFrame(metadata: ScreenFrameMetadata, payload: Buffer): void {
    if (
      this.stopped ||
      !this.session ||
      metadata.video_source !== "screen"
    ) {
      return;
    }
    this.latestFrame = { metadata: { ...metadata }, payload: Buffer.from(payload) };
    if (!this.processing) void this.drain();
  }

  handleBrokerControl(control: Record<string, unknown>): boolean {
    const callId = typeof control.call_id === "string" ? control.call_id : "";
    const pending = this.pendingActions.get(callId);
    if (!this.ownedCalls.has(callId)) return false;
    if (control.type === "tool.response") {
      this.ownedCalls.delete(callId);
      this.pendingActions.delete(callId);
      if (!pending) return true;
      const content =
        control.content && typeof control.content === "object"
          ? (control.content as JsonObject)
          : {};
      pending.resolve(content);
    }
    return true;
  }

  async close(): Promise<void> {
    if (this.stopped) return;
    this.stopped = true;
    this.latestFrame = null;
    for (const pending of this.pendingActions.values()) {
      pending.reject(new Error("desktop visual connector closed"));
    }
    this.pendingActions.clear();
    try {
      await this.stream?.close();
    } catch (error) {
      this.opts.log(
        "visual",
        `desktop visual stream did not close cleanly: ${String((error as Error).message ?? error)}`,
      );
    }
    try {
      if (this.host && this.session) {
        await this.host.closeSession(this.session.sessionId);
      }
    } catch (error) {
      this.opts.log(
        "visual",
        `desktop visual session did not close cleanly: ${String((error as Error).message ?? error)}`,
      );
    }
    this.stream = null;
    this.planner = null;
    this.host = null;
    this.session = null;
  }

  private async drain(): Promise<void> {
    this.processing = true;
    try {
      while (!this.stopped && this.latestFrame) {
        const frame = this.latestFrame;
        this.latestFrame = null;
        await this.processFrame(frame);
      }
    } catch (error) {
      this.opts.log(
        "visual",
        `desktop visual connector stopped: ${String((error as Error).message ?? error)}`,
      );
      await this.close();
    } finally {
      this.processing = false;
    }
  }

  private async processFrame(frame: PendingFrame): Promise<void> {
    if (!this.session || !this.host || !this.planner || !this.stream) {
      throw new Error("desktop visual connector is not started");
    }
    const { metadata, payload } = frame;
    await this.stream.sendFrame({
      frameId: metadata.frame_id,
      capturedAtMs: metadata.captured_at_ms,
      image: payload,
      encoding: metadata.encoding,
      videoSource: "screen",
      metadata: {
        source_kind: "desktop_live_screen",
        ...(metadata.width === undefined ? {} : { source_width: metadata.width }),
        ...(metadata.height === undefined ? {} : { source_height: metadata.height }),
      },
    });

    const response = await this.planner.decide(this.session.sessionId);
    const decisionId = requiredString(response, "decision_id");
    const decision = requiredObject(response, "decision");
    const action = requiredString(decision, "action") as DesktopVisualAction;
    if (!DESKTOP_VISUAL_ACTIONS.includes(action)) {
      throw new Error(`visual API returned unsupported desktop action ${action}`);
    }
    if (action === "done") {
      try {
        await this.host.recordAttempt(this.session.sessionId, decisionId);
        this.opts.log("visual", "desktop visual task completed");
      } finally {
        await this.close();
      }
      return;
    }

    try {
      if (action === "wait") {
        await new Promise((resolve) => setTimeout(resolve, 500));
        return;
      }
      const tool = toToolCall(action, decision, metadata);
      const result = await this.execute(decisionId, tool);
      this.opts.log(
        "visual",
        `desktop visual action ${action} → ${String(result.status ?? "unknown")}`,
      );
    } finally {
      await this.host.recordAttempt(this.session.sessionId, decisionId);
    }
  }

  private execute(
    decisionId: string,
    tool: { name: string; arguments: JsonObject },
  ): Promise<JsonObject> {
    const callId = `visual:${decisionId}`;
    if (this.pendingActions.has(callId)) {
      throw new Error(`duplicate desktop visual call ${callId}`);
    }
    return new Promise<JsonObject>((resolve, reject) => {
      this.pendingActions.set(callId, { resolve, reject });
      this.ownedCalls.add(callId);
      const turnId = this.opts.latestTurnId();
      const handled = this.opts.dispatch({
        type: "tool.call",
        dispatch: "client",
        call_id: callId,
        turn_id: turnId,
        provenance: turnId ? "mixed" : "unknown",
        capability_manifest_id: this.opts.capabilityManifestId,
        tool_calls: [tool],
      });
      if (!handled) {
        this.pendingActions.delete(callId);
        this.ownedCalls.delete(callId);
        reject(new Error(`desktop action broker refused call ${callId}`));
      }
    });
  }
}

function defaultServices(baseUrl: string, hostToken: string): VisualServices {
  const host = new VisualClient({ baseUrl, apiToken: hostToken });
  return {
    host,
    planner: (session) =>
      new VisualClient({ baseUrl, apiToken: session.plannerToken }),
    stream: (session) => FrameStream.connect(baseUrl, session),
  };
}

function toToolCall(
  action: DesktopVisualAction,
  decision: JsonObject,
  frame: ScreenFrameMetadata,
): { name: string; arguments: JsonObject } {
  switch (action) {
    case "click": {
      const target = requiredObject(decision, "target");
      const width = positiveNumber(frame.width, "screen frame width");
      const height = positiveNumber(frame.height, "screen frame height");
      return {
        name: "input",
        arguments: {
          action: "click",
          x: boundedCoordinate(target.x, width, "x"),
          y: boundedCoordinate(target.y, height, "y"),
        },
      };
    }
    case "type":
      return {
        name: "input",
        arguments: { action: "type", text: requiredString(decision, "text") },
      };
    case "navigate":
      return {
        name: "browser",
        arguments: { action: "go", url: requiredString(decision, "url") },
      };
    case "back":
      return { name: "browser", arguments: { action: "back" } };
    default:
      throw new Error(`desktop action ${action} does not map to a host tool`);
  }
}

function boundedCoordinate(
  value: unknown,
  extent: number,
  name: string,
): number {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new Error(`desktop click ${name} must be a finite number`);
  }
  if (value < 0 || value > extent) {
    throw new Error(`desktop click ${name} is outside the live screen frame`);
  }
  return Math.min(1, Math.max(0, value / extent));
}

function positiveNumber(value: unknown, name: string): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) {
    throw new Error(`${name} must be a positive number`);
  }
  return value;
}

function requiredObject(value: JsonObject, key: string): JsonObject {
  const item = value[key];
  if (!item || typeof item !== "object" || Array.isArray(item)) {
    throw new Error(`${key} must be an object`);
  }
  return item as JsonObject;
}

function requiredString(value: JsonObject, key: string): string {
  const item = value[key];
  if (typeof item !== "string" || !item.trim()) {
    throw new Error(`${key} must be a non-empty string`);
  }
  return item;
}
