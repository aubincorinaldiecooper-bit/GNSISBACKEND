/**
 * Which runtimes this desktop will take action requests from.
 *
 * Offering actions hands the runtime the ability to ask this machine to open,
 * move, type and click — and to read file names and tab titles back. So they
 * are offered only to GNSIS's own site over TLS, or to a runtime on this very
 * machine, unless the person has said otherwise for the address they chose
 * (`"actions": true` in gnsis.json, or GNSIS_ACTIONS=on). GNSIS_ACTIONS=off,
 * or `"actions": false`, turns them off everywhere.
 */
export type ActionsSetting = "on" | "off" | undefined;

export function actionsAllowed(runtimeUrl: string, setting: ActionsSetting): { allowed: boolean; why: string } {
  if (setting === "off") return { allowed: false, why: "turned off in settings" };
  if (setting === "on") return { allowed: true, why: "turned on in settings" };
  let url: URL;
  try {
    url = new URL(runtimeUrl);
  } catch {
    return { allowed: false, why: "the runtime address is not a URL" };
  }
  const host = url.hostname.toLowerCase();
  if (host === "127.0.0.1" || host === "localhost" || host === "[::1]" || host === "::1") {
    return { allowed: true, why: "the runtime is on this machine" };
  }
  if ((url.protocol === "https:" || url.protocol === "wss:") && (host === "gnsis.studio" || host.endsWith(".gnsis.studio"))) {
    return { allowed: true, why: "the runtime is GNSIS's own, over TLS" };
  }
  return { allowed: false, why: `${host} is not a runtime this desktop takes actions from` };
}
