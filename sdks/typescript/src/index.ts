export class VisualServiceError extends Error {
  constructor(
    public readonly code: string,
    message: string,
    public readonly status: number | null = null,
    public readonly retryable = false,
  ) {
    super(message);
    this.name = "VisualServiceError";
  }
}

/** Host stream credentials and the planner token scoped to this session. */
export interface VisualSession {
  sessionId: string;
  streamPath: string;
  streamToken: string;
  plannerToken: string;
  protocol: string;
}

/** A current-viewport pixel to ground; nothing is drawn onto the frame. */
/** Viewport-pixel rectangle of a retained frame. */
export interface Region {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface TargetPoint {
  x: number;
  y: number;
}

export interface VisualClientOptions {
  baseUrl: string;
  apiToken: string;
  timeoutMs?: number;
  maxRetries?: number;
  fetch?: typeof fetch;
}

type JsonObject = Record<string, unknown>;

function isJsonObject(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isRetryableStatus(status: number): boolean {
  return status === 502 || status === 503 || status === 504;
}

function isTransportError(error: unknown): boolean {
  if (error instanceof TypeError) return true;
  return (
    typeof error === "object" &&
    error !== null &&
    ["AbortError", "TimeoutError"].includes(
      String((error as { name?: unknown }).name ?? ""),
    )
  );
}

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function redactSecret(value: string, secret: string): string {
  return secret ? value.split(secret).join("[redacted]") : value;
}

function validateBaseUrl(baseUrl: string): URL {
  const scheme =
    /^([A-Za-z][A-Za-z\d+.-]*):/.exec(baseUrl)?.[1]?.toLowerCase() ?? "";
  if (scheme !== "https" && scheme !== "http") {
    throw new VisualServiceError(
      "insecure_transport",
      "visual API requires HTTPS except for loopback hosts",
    );
  }
  let url: URL;
  try {
    url = new URL(baseUrl);
  } catch {
    throw new VisualServiceError(
      "invalid_base_url",
      "visual API base URL is invalid",
    );
  }
  const hostname = url.hostname.replace(/^\[|\]$/g, "").toLowerCase();
  if (
    url.protocol === "http:" &&
    !["localhost", "127.0.0.1", "::1"].includes(hostname)
  ) {
    throw new VisualServiceError(
      "insecure_transport",
      "visual API requires HTTPS except for loopback hosts",
    );
  }
  return url;
}

function encodePathSegment(segment: string): string {
  return encodeURIComponent(segment).replace(/[!'()*]/g, (character) =>
    `%${character.charCodeAt(0).toString(16).toUpperCase()}`,
  );
}

export class VisualClient {
  readonly #baseUrl: string;
  readonly #apiToken: string;
  readonly #timeoutMs: number;
  readonly #maxRetries: number;
  readonly #fetch: typeof fetch;

  constructor(options: VisualClientOptions) {
    validateBaseUrl(options.baseUrl);
    this.#baseUrl = options.baseUrl.replace(/\/+$/, "");
    this.#apiToken = options.apiToken;
    this.#timeoutMs = options.timeoutMs ?? 30_000;
    this.#maxRetries = options.maxRetries ?? 2;
    this.#fetch = (options.fetch ?? globalThis.fetch).bind(globalThis);

    if (!Number.isFinite(this.#timeoutMs) || this.#timeoutMs <= 0) {
      throw new RangeError("timeoutMs must be a positive number");
    }
    if (!Number.isInteger(this.#maxRetries) || this.#maxRetries < 0) {
      throw new RangeError("maxRetries must be a non-negative integer");
    }
  }

  toString(): string {
    return "VisualClient";
  }

  async health(): Promise<JsonObject> {
    return this.#request("GET", "/health", undefined, false, true);
  }

  /**
   * Create a session with a host token; its planner token is session-scoped.
   */
  async createSession(): Promise<VisualSession> {
    const payload = await this.#request(
      "POST",
      "/v1/visual/sessions",
      {},
      true,
      false,
    );
    const stream = isJsonObject(payload.stream) ? payload.stream : {};
    const planner = isJsonObject(payload.planner) ? payload.planner : {};
    return {
      sessionId: String(payload.session_id),
      streamPath: String(stream.path),
      streamToken: String(stream.token),
      plannerToken: String(planner.token),
      protocol: String(stream.protocol),
    };
  }

  /** Close a session with the host token. */
  async closeSession(sessionId: string): Promise<JsonObject> {
    return this.#request(
      "DELETE",
      this.#sessionPath(sessionId),
      undefined,
      true,
      true,
      true,
    );
  }

  async setTask(
    sessionId: string,
    goal: string,
    allowedActions?: readonly string[],
  ): Promise<JsonObject> {
    const body: JsonObject = { goal };
    if (allowedActions !== undefined) body.allowed_actions = allowedActions;
    return this.#request(
      "PUT",
      `${this.#sessionPath(sessionId)}/task`,
      body,
      true,
      true,
    );
  }

  async resetTask(sessionId: string): Promise<JsonObject> {
    return this.#request(
      "DELETE",
      `${this.#sessionPath(sessionId)}/task`,
      undefined,
      true,
      true,
    );
  }

  async decide(sessionId: string, requestId?: string): Promise<JsonObject> {
    const stableRequestId = requestId ?? crypto.randomUUID();
    return this.#request(
      "POST",
      `${this.#sessionPath(sessionId)}/decisions`,
      { request_id: stableRequestId },
      true,
      true,
    );
  }

  async perceive(
    sessionId: string,
    requestId?: string,
    focus?: string,
    target?: TargetPoint,
  ): Promise<JsonObject> {
    const stableRequestId = requestId ?? crypto.randomUUID();
    return this.#request(
      "POST",
      `${this.#sessionPath(sessionId)}/perceptions`,
      {
        request_id: stableRequestId,
        ...(focus === undefined ? {} : { focus }),
        ...(target === undefined ? {} : { target }),
      },
      true,
      true,
    );
  }

  /** Retained frames and earlier perceptions, newest first. */
  async history(sessionId: string, limit?: number): Promise<JsonObject> {
    const query = limit === undefined ? "" : `?limit=${Math.trunc(limit)}`;
    return this.#request(
      "GET",
      `${this.#sessionPath(sessionId)}/history${query}`,
      undefined,
      true,
      true,
    );
  }

  /** A full-resolution PNG crop (base64) of a retained frame's region. */
  async inspect(
    sessionId: string,
    options: { frameId?: string; region?: Region; displaySize?: number } = {},
  ): Promise<JsonObject> {
    return this.#request(
      "POST",
      `${this.#sessionPath(sessionId)}/inspections`,
      {
        ...(options.frameId === undefined ? {} : { frame_id: options.frameId }),
        ...(options.region === undefined ? {} : { region: { ...options.region } }),
        ...(options.displaySize === undefined
          ? {}
          : { display_size: options.displaySize }),
      },
      true,
      true,
    );
  }

  /** Exact `#rrggbb` samples from a retained frame's region. */
  async readPixels(
    sessionId: string,
    region: Region,
    options: { frameId?: string; step?: number } = {},
  ): Promise<JsonObject> {
    return this.#request(
      "POST",
      `${this.#sessionPath(sessionId)}/pixels`,
      {
        region: { ...region },
        step: options.step ?? 1,
        ...(options.frameId === undefined ? {} : { frame_id: options.frameId }),
      },
      true,
      true,
    );
  }

  /** Record an execution attempt with the host token. */
  async recordAttempt(
    sessionId: string,
    decisionId: string,
  ): Promise<JsonObject> {
    return this.#request(
      "POST",
      `${this.#sessionPath(sessionId)}/attempts`,
      { decision_id: decisionId },
      true,
      false,
    );
  }

  async state(sessionId: string): Promise<JsonObject> {
    return this.#request(
      "GET",
      this.#sessionPath(sessionId),
      undefined,
      true,
      true,
    );
  }

  #sessionPath(sessionId: string): string {
    return `/v1/visual/sessions/${encodePathSegment(sessionId)}`;
  }

  async #request(
    method: string,
    path: string,
    body: JsonObject | undefined,
    authenticated: boolean,
    retry: boolean,
    acceptUnknownSessionAfterRetry = false,
  ): Promise<JsonObject> {
    const headers = new Headers();
    try {
      if (body !== undefined) headers.set("content-type", "application/json");
      if (authenticated) {
        headers.set("authorization", `Bearer ${this.#apiToken}`);
      }
    } catch {
      throw new VisualServiceError(
        "invalid_request",
        "visual API request could not be configured",
      );
    }

    for (let attempt = 0; ; attempt += 1) {
      let response: Response;
      try {
        response = await this.#fetch(`${this.#baseUrl}${path}`, {
          method,
          headers,
          body: body === undefined ? undefined : JSON.stringify(body),
          signal: AbortSignal.timeout(this.#timeoutMs),
        });
      } catch (error) {
        if (isTransportError(error)) {
          if (retry && attempt < this.#maxRetries) {
            await delay(200 * 2 ** attempt);
            continue;
          }
          throw new VisualServiceError(
            "transport_error",
            "visual API transport failed",
            null,
            true,
          );
        }
        throw new VisualServiceError(
          "request_error",
          "visual API request failed",
          null,
          false,
        );
      }

      if (isRetryableStatus(response.status) && retry && attempt < this.#maxRetries) {
        await delay(200 * 2 ** attempt);
        continue;
      }

      let payload: unknown;
      try {
        payload = await response.json();
      } catch {
        payload = {};
      }

      if (!response.ok) {
        const errorBody = isJsonObject(payload)
          ? isJsonObject(payload.error)
            ? payload.error
            : isJsonObject(payload.detail)
              ? payload.detail
              : {}
          : {};
        const message = redactSecret(
          String(errorBody.message ?? response.statusText ?? "request failed"),
          this.#apiToken,
        );
        const code = redactSecret(
          String(errorBody.code ?? "http_error"),
          this.#apiToken,
        );
        if (
          acceptUnknownSessionAfterRetry &&
          attempt > 0 &&
          response.status === 404 &&
          errorBody.code === "unknown_session"
        ) {
          return { closed: true };
        }
        throw new VisualServiceError(
          code,
          message,
          response.status,
          isRetryableStatus(response.status),
        );
      }

      if (!isJsonObject(payload)) {
        throw new VisualServiceError(
          "invalid_response",
          "visual API returned an invalid JSON response",
          response.status,
          false,
        );
      }
      return payload;
    }
  }
}

export interface SendFrameOptions {
  frameId: string;
  capturedAtMs: number;
  image: Uint8Array;
  encoding?: string;
  videoSource?: string;
  metadata?: JsonObject;
}

export class FrameStream {
  readonly #socket: WebSocket;
  readonly #streamToken: string;
  #pending: Promise<void> = Promise.resolve();

  private constructor(socket: WebSocket, streamToken: string) {
    this.#socket = socket;
    this.#streamToken = streamToken;
  }

  static async connect(
    baseUrl: string,
    session: VisualSession,
  ): Promise<FrameStream> {
    const base = validateBaseUrl(baseUrl);
    let url: URL;
    try {
      url = new URL(session.streamPath, base);
      url.protocol = base.protocol === "https:" ? "wss:" : "ws:";
      url.searchParams.set("token", session.streamToken);
    } catch {
      throw new VisualServiceError(
        "invalid_stream_url",
        "visual stream URL is invalid",
      );
    }

    let socket: WebSocket;
    try {
      socket = new WebSocket(url);
    } catch {
      throw new VisualServiceError(
        "stream_connect_error",
        "visual stream connection failed",
        null,
        true,
      );
    }
    try {
      await new Promise<void>((resolve, reject) => {
        const onOpen = () => {
          cleanup();
          resolve();
        };
        const onError = () => {
          cleanup();
          reject(
            new VisualServiceError(
              "stream_connect_error",
              "visual stream connection failed",
              null,
              true,
            ),
          );
        };
        const onClose = () => {
          cleanup();
          reject(
            new VisualServiceError(
              "stream_connect_error",
              "visual stream closed before connecting",
              null,
              true,
            ),
          );
        };
        const cleanup = () => {
          socket.removeEventListener("open", onOpen);
          socket.removeEventListener("error", onError);
          socket.removeEventListener("close", onClose);
        };
        socket.addEventListener("open", onOpen, { once: true });
        socket.addEventListener("error", onError, { once: true });
        socket.addEventListener("close", onClose, { once: true });
      });
    } catch (error) {
      socket.close();
      if (error instanceof VisualServiceError) throw error;
      throw new VisualServiceError(
        "stream_connect_error",
        "visual stream connection failed",
        null,
        true,
      );
    }
    return new FrameStream(socket, session.streamToken);
  }

  toString(): string {
    return "FrameStream";
  }

  sendFrame(options: SendFrameOptions): Promise<JsonObject> {
    const operation = this.#pending.then(() => this.#sendFrame(options));
    this.#pending = operation.then(
      () => undefined,
      () => undefined,
    );
    return operation;
  }

  async close(): Promise<void> {
    await this.#pending;
    if (this.#socket.readyState === WebSocket.OPEN) {
      this.#socket.close();
    }
  }

  async #sendFrame(options: SendFrameOptions): Promise<JsonObject> {
    if (this.#socket.readyState !== WebSocket.OPEN) {
      throw new VisualServiceError(
        "stream_closed",
        "visual stream is not open",
        null,
        true,
      );
    }

    const header = JSON.stringify({
      type: "screen.frame",
      frame_id: options.frameId,
      captured_at_ms: options.capturedAtMs,
      encoding: options.encoding ?? "jpeg",
      video_source: options.videoSource ?? "screen",
      ...(options.metadata === undefined ? {} : { metadata: options.metadata }),
    });

    return new Promise<JsonObject>((resolve, reject) => {
      const cleanup = () => {
        this.#socket.removeEventListener("message", onMessage);
        this.#socket.removeEventListener("error", onError);
        this.#socket.removeEventListener("close", onClose);
      };
      const fail = (error: VisualServiceError) => {
        cleanup();
        reject(error);
      };
      const onMessage = (event: MessageEvent<unknown>) => {
        cleanup();
        if (typeof event.data !== "string") {
          reject(
            new VisualServiceError(
              "invalid_stream_response",
              "visual stream returned a non-text response",
            ),
          );
          return;
        }
        let payload: unknown;
        try {
          payload = JSON.parse(event.data);
        } catch {
          reject(
            new VisualServiceError(
              "invalid_stream_response",
              "visual stream returned invalid JSON",
            ),
          );
          return;
        }
        if (!isJsonObject(payload)) {
          reject(
            new VisualServiceError(
              "invalid_stream_response",
              "visual stream returned an invalid response",
            ),
          );
          return;
        }
        if (payload.type === "screen.frame.accepted") {
          resolve(payload);
          return;
        }
        if (payload.type === "screen.frame.rejected") {
          const error = isJsonObject(payload.error) ? payload.error : {};
          const code = redactSecret(
            String(error.code ?? "frame_rejected"),
            this.#streamToken,
          );
          const message = redactSecret(
            String(error.message ?? "frame was rejected"),
            this.#streamToken,
          );
          reject(
            new VisualServiceError(
              code,
              message,
              null,
              false,
            ),
          );
          return;
        }
        reject(
          new VisualServiceError(
            "invalid_stream_response",
            "visual stream returned an unknown response",
          ),
        );
      };
      const onError = () =>
        fail(
          new VisualServiceError(
            "stream_error",
            "visual stream encountered an error",
            null,
            true,
          ),
        );
      const onClose = () =>
        fail(
          new VisualServiceError(
            "stream_closed",
            "visual stream closed before replying",
            null,
            true,
          ),
        );

      this.#socket.addEventListener("message", onMessage, { once: true });
      this.#socket.addEventListener("error", onError, { once: true });
      this.#socket.addEventListener("close", onClose, { once: true });
      try {
        this.#socket.send(header);
        this.#socket.send(new Uint8Array(options.image));
      } catch {
        fail(
          new VisualServiceError(
            "stream_send_error",
            "visual frame could not be sent",
            null,
            true,
          ),
        );
      }
    });
  }
}
