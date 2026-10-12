import type {
  LocalControllerContext,
  LocalControllerDecision,
  LocalDecisionProvider,
} from "./localController.js";

const MAX_RESPONSE_BYTES = 64 * 1024;

type FetchRequest = (
  input: string | URL,
  init?: RequestInit,
) => Promise<Response>;

/** Kitten TTS 2 speech from the same loopback Genesis 2.0 process. */
export async function localSpeech(
  baseUrl: string,
  text: string,
  token?: string,
  fetchFn: typeof fetch = fetch,
): Promise<Buffer> {
  const response = await fetchFn(new URL("/v1/speak", loopbackUrl(baseUrl)), {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify({ text }),
    signal: AbortSignal.timeout(60_000),
  });
  if (!response.ok) throw new Error(`local speech answered HTTP ${response.status}`);
  const pcm = Buffer.from(await response.arrayBuffer());
  if (pcm.length === 0 || pcm.length % 2 !== 0 || pcm.length > 24_000 * 2 * 120) {
    throw new Error("local speech returned invalid audio");
  }
  return pcm;
}

export interface HttpLocalDecisionProviderOptions {
  baseUrl: string;
  tools: readonly string[];
  toolSchemas?: readonly unknown[];
  token?: string;
  request?: FetchRequest;
  timeoutMs?: number;
}

function loopbackUrl(value: string): URL {
  const url = new URL(value);
  const host = url.hostname.toLowerCase();
  if (
    url.protocol !== "http:" ||
    (host !== "127.0.0.1" &&
      host !== "localhost" &&
      host !== "[::1]" &&
      host !== "::1")
  ) {
    throw new Error("The local controller must use an HTTP loopback address.");
  }
  return url;
}

function plainObject(value: unknown): value is Record<string, unknown> {
  return (
    typeof value === "object" &&
    value !== null &&
    !Array.isArray(value) &&
    Object.getPrototypeOf(value) === Object.prototype
  );
}

function shortString(value: unknown): string | undefined {
  return typeof value === "string" && value.length <= 2_000
    ? value
    : undefined;
}

function parseDecision(value: unknown): LocalControllerDecision {
  if (!plainObject(value)) {
    throw new Error("The local controller returned an invalid decision.");
  }
  if (value.kind === "tool") {
    if (
      typeof value.tool !== "string" ||
      !plainObject(value.args) ||
      typeof value.confidence !== "number"
    ) {
      throw new Error("The local controller returned an invalid tool decision.");
    }
    return {
      kind: "tool",
      tool: value.tool,
      args: value.args,
      confidence: value.confidence,
    };
  }
  if (value.kind === "done") {
    const message = shortString(value.message);
    if (value.message !== undefined && message === undefined) {
      throw new Error("The local controller returned an invalid completion.");
    }
    return { kind: "done", ...(message ? { message } : {}) };
  }
  if (value.kind === "fallback") {
    const reason = shortString(value.reason);
    if (value.reason !== undefined && reason === undefined) {
      throw new Error("The local controller returned an invalid fallback.");
    }
    return { kind: "fallback", ...(reason ? { reason } : {}) };
  }
  throw new Error("The local controller returned an unknown decision.");
}

export class HttpLocalDecisionProvider implements LocalDecisionProvider {
  private readonly endpoint: URL;
  private readonly request: FetchRequest;

  constructor(private readonly opts: HttpLocalDecisionProviderOptions) {
    this.endpoint = new URL("/v1/decide", loopbackUrl(opts.baseUrl));
    this.request = opts.request ?? fetch;
  }

  async decide(
    context: LocalControllerContext,
  ): Promise<LocalControllerDecision> {
    const response = await this.request(this.endpoint, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(this.opts.token
          ? { Authorization: `Bearer ${this.opts.token}` }
          : {}),
      },
      body: JSON.stringify({
        ...context,
        tools: this.opts.tools,
        tool_schemas: this.opts.toolSchemas ?? [],
      }),
      signal: AbortSignal.timeout(this.opts.timeoutMs ?? 20_000),
    });
    if (!response.ok) {
      throw new Error(`The local controller returned ${response.status}.`);
    }
    const body = await response.text();
    if (Buffer.byteLength(body, "utf8") > MAX_RESPONSE_BYTES) {
      throw new Error("The local controller response was too large.");
    }
    return parseDecision(JSON.parse(body) as unknown);
  }
}
