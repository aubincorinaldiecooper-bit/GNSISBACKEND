/**
 * Desktop tools seam.
 *
 * The desktop shell owns device perception; tools are where search and
 * actions plug in behind it. A tool is name + schema + a call that goes to a
 * backend endpoint — the registry validates args, forwards over HTTP, and
 * returns a normalized result for the model/control path.
 *
 * `internet_search` is the first adapter: it reuses the SearXNG-backed
 * discovery pattern from the video-retrieval service (query in, ranked
 * results out), pointing at whatever GNSIS backend or SearXNG instance
 * GNSIS_SEARCH_URL names. Adding an action tool is register(), not plumbing.
 */

export interface ToolResult {
  ok: boolean;
  tool: string;
  result?: unknown;
  error?: string;
  latency_ms: number;
}

export interface ToolSpec {
  name: string;
  description: string;
  call(args: Record<string, unknown>): Promise<unknown>;
  validate?(args: Record<string, unknown>): void;
}

export class ToolRegistry {
  private readonly specs = new Map<string, ToolSpec>();
  private readonly runtimeUrl: string;
  private readonly searchUrl: string;

  constructor(opts: { runtimeUrl: string }) {
    this.runtimeUrl = opts.runtimeUrl;
    this.searchUrl = process.env.GNSIS_SEARCH_URL ?? "";
    this.register(internetSearchSpec(this.searchUrl));
  }

  register(spec: ToolSpec): void {
    if (this.specs.has(spec.name)) throw new Error(`duplicate tool ${spec.name}`);
    this.specs.set(spec.name, spec);
  }

  list(): Array<{ name: string; description: string }> {
    return [...this.specs.values()].map((s) => ({
      name: s.name,
      description: s.description,
    }));
  }

  async call(name: string, args: Record<string, unknown>): Promise<ToolResult> {
    const spec = this.specs.get(name);
    const started = Date.now();
    if (!spec) {
      return { ok: false, tool: name, error: `unknown tool ${name}`, latency_ms: 0 };
    }
    try {
      spec.validate?.(args);
      const result = await spec.call(args);
      return { ok: true, tool: name, result, latency_ms: Date.now() - started };
    } catch (err) {
      return {
        ok: false,
        tool: name,
        error: err instanceof Error ? err.message : String(err),
        latency_ms: Date.now() - started,
      };
    }
  }
}

function internetSearchSpec(searchUrl: string): ToolSpec {
  return {
    name: "internet_search",
    description: "Search the web via the configured SearXNG endpoint",
    validate(args) {
      if (typeof args.query !== "string" || !args.query.trim()) {
        throw new Error("internet_search requires a non-empty query");
      }
    },
    async call(args) {
      if (!searchUrl) {
        throw new Error("GNSIS_SEARCH_URL is not configured");
      }
      const url = `${searchUrl.replace(/\/$/, "")}/search?q=${encodeURIComponent(
        String(args.query),
      )}&format=json`;
      const resp = await fetch(url);
      if (!resp.ok) throw new Error(`search backend -> ${resp.status}`);
      const body = (await resp.json()) as { results?: unknown[] };
      return { query: args.query, results: body.results ?? [] };
    },
  };
}
