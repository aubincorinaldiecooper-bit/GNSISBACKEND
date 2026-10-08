/**
 * Thin, lazy wrapper around Cua Driver.
 *
 * GNSIS owns policy, approvals and visual verification. Cua owns low-level
 * desktop/browser actuation and accessibility state. We deliberately do not
 * call Cua's screenshot surfaces here: Panoptic's persistent screen stream is
 * the visual source of truth.
 */
export type CuaArgs = Record<string, unknown>;

export interface CuaResponse {
  text: string;
  structured?: unknown;
  errorCode?: string;
  verified?: boolean;
  degraded?: boolean;
}

export interface CuaControl {
  call(tool: string, args: CuaArgs): Promise<CuaResponse>;
  /**
   * Opens Cua's protected existing-profile boundary for one exact browser
   * window while `run` executes. GNSIS must have approved that high-level
   * action before calling this.
   */
  withExistingProfileAuthorization?<T>(
    pid: number,
    windowId: number,
    run: () => Promise<T>,
  ): Promise<T>;
  close?(): Promise<void>;
}

type NativeToolResult = {
  text?: string;
  structuredJson?: string | null;
  structured_json?: string | null;
  rawJson?: string | null;
  raw_json?: string | null;
  isError?: boolean;
  is_error?: boolean;
  errorCode?: string | null;
  error_code?: string | null;
  verified?: boolean;
  degraded?: boolean;
};

type NativeDriver = {
  callTool(name: string, argumentsJson: string): Promise<NativeToolResult>;
  shutdown(): Promise<void>;
  uniffiDestroy?: () => void;
};

export class CuaToolError extends Error {
  constructor(
    readonly tool: string,
    message: string,
    readonly code?: string,
    readonly structured?: unknown,
  ) {
    super(message);
  }
}

export class NativeCuaControl implements CuaControl {
  private driver: NativeDriver | null = null;
  private loading: Promise<NativeDriver> | null = null;
  private profileAuthorization: { pid: number; windowId: number; expiresAtMs: number } | null = null;

  private async getDriver(): Promise<NativeDriver> {
    if (this.driver) return this.driver;
    if (!this.loading) {
      this.loading = import("@trycua/cua-driver").then((mod) => {
        // Standard mode keeps Cua promptless for ordinary automation. The only
        // protected boundary we open from GNSIS is existing-profile browser
        // attachment, and only while withExistingProfileAuthorization() holds
        // an exact pid/window lease after GNSIS has asked the person.
        const driver = mod.CuaDriver.createConfiguredWithAuthorizationHost(
          {
            claudeCodeCompatibility: false,
            authorization: {
              allowedModes: [mod.SessionPermissionMode.Standard],
              compatibilityMode: mod.SessionPermissionMode.Standard,
              compatibilityCapabilityManifestPath: undefined,
              compatibilityBoundedManifestPath: undefined,
              unrestrictedAcknowledged: false,
              maxSessionTtlSeconds: 28_800n,
              maxIdleTtlSeconds: 1_800n,
            },
          },
          {
            authorize: async (request) => {
              const lease = this.profileAuthorization;
              let exact = false;
              if (
                lease &&
                Date.now() <= lease.expiresAtMs &&
                request.adapterId === "browser_prepare.existing_profile"
              ) {
                try {
                  const resource = JSON.parse(request.resourceJson) as {
                    pid?: unknown;
                    window_id?: unknown;
                    windowId?: unknown;
                  };
                  exact =
                    Number(resource.pid) === lease.pid &&
                    Number(resource.window_id ?? resource.windowId) === lease.windowId;
                } catch {
                  exact = false;
                }
              }
              return {
                action: exact
                  ? mod.DriverAuthorizationAction.Allow
                  : mod.DriverAuthorizationAction.Deny,
                requestDigest: request.requestDigest,
              };
            },
          },
        ) as unknown as NativeDriver;
        this.driver = driver;
        return driver;
      });
    }
    return this.loading;
  }

  async call(tool: string, args: CuaArgs): Promise<CuaResponse> {
    const driver = await this.getDriver();
    const result = await driver.callTool(tool, JSON.stringify(args));
    const structuredText = result.structuredJson ?? result.structured_json ?? null;
    let structured: unknown;
    if (structuredText) {
      try {
        structured = JSON.parse(structuredText);
      } catch {
        structured = undefined;
      }
    }
    const isError = result.isError === true || result.is_error === true;
    const code = result.errorCode ?? result.error_code ?? undefined;
    const text = result.text ?? (isError ? `${tool} failed` : `${tool} completed`);
    if (isError) throw new CuaToolError(tool, text, code ?? undefined, structured);
    return {
      text,
      structured,
      errorCode: code ?? undefined,
      verified: result.verified,
      degraded: result.degraded,
    };
  }

  async withExistingProfileAuthorization<T>(
    pid: number,
    windowId: number,
    run: () => Promise<T>,
  ): Promise<T> {
    if (this.profileAuthorization) {
      throw new CuaToolError(
        "browser_prepare",
        "Another protected browser attachment is already being authorized.",
        "authorization_busy",
      );
    }
    const lease = { pid, windowId, expiresAtMs: Date.now() + 30_000 };
    this.profileAuthorization = lease;
    try {
      return await run();
    } finally {
      if (this.profileAuthorization === lease) this.profileAuthorization = null;
    }
  }

  async close(): Promise<void> {
    const driver = this.driver;
    this.driver = null;
    this.loading = null;
    if (!driver) return;
    try {
      await driver.shutdown();
    } finally {
      driver.uniffiDestroy?.();
    }
  }
}

export interface CuaWindow {
  pid: number;
  windowId: number;
  appName: string;
  title: string;
  bounds?: { x: number; y: number; width: number; height: number };
  zIndex: number | null;
  onScreen: boolean;
}

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function number(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function string(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function bool(value: unknown, fallback: boolean): boolean {
  return typeof value === "boolean" ? value : fallback;
}

export async function listWindows(
  cua: CuaControl,
  opts: { pid?: number; onScreenOnly?: boolean } = {},
): Promise<CuaWindow[]> {
  const response = await cua.call("list_windows", {
    ...(opts.pid ? { pid: opts.pid } : {}),
    ...(opts.onScreenOnly === undefined ? {} : { on_screen_only: opts.onScreenOnly }),
  });
  const root = response.structured;
  const raw = Array.isArray(root)
    ? root
    : Array.isArray(record(root)?.windows)
      ? (record(root)!.windows as unknown[])
      : [];
  return raw.flatMap((item) => {
    const row = record(item);
    if (!row) return [];
    const pid = number(row.pid);
    const windowId = number(row.window_id ?? row.windowId);
    if (!pid || !windowId) return [];
    const b = record(row.bounds);
    const bounds =
      b && [b.x, b.y, b.width, b.height].every((v) => typeof v === "number")
        ? { x: b.x as number, y: b.y as number, width: b.width as number, height: b.height as number }
        : undefined;
    return [{
      pid,
      windowId,
      appName: string(row.app_name ?? row.appName),
      title: string(row.title),
      bounds,
      zIndex: number(row.z_index ?? row.zIndex),
      onScreen: bool(row.is_on_screen ?? row.onScreen, true),
    }];
  });
}

function includes(haystack: string, needle: string): boolean {
  return haystack.toLocaleLowerCase().includes(needle.toLocaleLowerCase());
}

export async function resolveWindow(
  cua: CuaControl,
  opts: { app?: string; window?: string; onScreenOnly?: boolean } = {},
): Promise<CuaWindow> {
  const windows = await listWindows(cua, { onScreenOnly: opts.onScreenOnly ?? true });
  let candidates = windows;
  if (opts.app) {
    const exact = candidates.filter((w) => w.appName.toLocaleLowerCase() === opts.app!.toLocaleLowerCase());
    candidates = exact.length ? exact : candidates.filter((w) => includes(w.appName, opts.app!));
  }
  if (opts.window) {
    const exact = candidates.filter((w) => w.title.toLocaleLowerCase() === opts.window!.toLocaleLowerCase());
    candidates = exact.length ? exact : candidates.filter((w) => includes(w.title, opts.window!));
  }
  if (!candidates.length) {
    const named = [opts.app, opts.window].filter(Boolean).join(" / ");
    throw new CuaToolError("list_windows", named ? `No window matched ${named}.` : "No on-screen window is available.", "not_found");
  }
  const withZ = candidates.filter((w) => w.zIndex !== null);
  if (withZ.length) return withZ.sort((a, b) => (b.zIndex ?? -1) - (a.zIndex ?? -1))[0];
  return candidates[0];
}

export function windowArgs(window: CuaWindow): Record<string, unknown> {
  return { pid: window.pid, window_id: window.windowId };
}

export function findElementToken(value: unknown, label: string): string | null {
  const want = label.trim().toLocaleLowerCase();
  const visit = (node: unknown): string | null => {
    if (Array.isArray(node)) {
      for (const item of node) {
        const found = visit(item);
        if (found) return found;
      }
      return null;
    }
    const row = record(node);
    if (!row) return null;
    const token = string(row.element_token ?? row.elementToken);
    const fields = [row.label, row.name, row.title, row.description, row.value]
      .filter((v): v is string => typeof v === "string")
      .map((v) => v.trim().toLocaleLowerCase());
    if (token && fields.some((v) => v === want)) return token;
    for (const child of Object.values(row)) {
      const found = visit(child);
      if (found) return found;
    }
    return null;
  };
  return visit(value);
}

export function collectTabLabels(value: unknown): Array<{ title: string; selected: boolean }> {
  const out: Array<{ title: string; selected: boolean }> = [];
  const seen = new Set<string>();
  const visit = (node: unknown): void => {
    if (Array.isArray(node)) {
      for (const item of node) visit(item);
      return;
    }
    const row = record(node);
    if (!row) return;
    const role = string(row.role).toLocaleLowerCase();
    const title = string(row.label ?? row.name ?? row.title);
    if (title && role.includes("tab") && !seen.has(title)) {
      seen.add(title);
      out.push({ title, selected: row.selected === true });
    }
    for (const child of Object.values(row)) visit(child);
  };
  visit(value);
  return out;
}
