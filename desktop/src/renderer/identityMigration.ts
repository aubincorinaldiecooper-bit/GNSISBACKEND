import type { Identity } from "@gnsis/ui";
import type { LocalIdentityStore } from "@gnsis/ui";
import type { GnsisBridge } from "./bridge.js";

const API_URL = "https://api.gnsis.studio";

interface ChallengeReply {
  challenge_id: string;
  challenge: string;
}

interface CompleteReply {
  grant: string;
  recovery_key?: string | null;
}

export async function migrateDesktopIdentity(
  store: LocalIdentityStore,
  bridge: GnsisBridge,
  log: (line: string) => void,
  knownIdentity?: Identity,
): Promise<void> {
  const identity = knownIdentity ?? (await store.load());
  if (!identity) return;
  if (!bridge.setDesktopGrant || !bridge.protectDesktopIdentity) return;
  try {
    const challenge = await post<ChallengeReply>("/v1/desktop/identity/challenge", {
      public_id: identity.publicId,
      public_key: identity.publicKey,
    });
    const signature = await store.signChallenge(challenge.challenge);
    const completed = await post<CompleteReply>("/v1/desktop/identity/complete", {
      challenge_id: challenge.challenge_id,
      signature,
    });
    const grant = await bridge.protectDesktopIdentity(
      completed.grant,
      completed.recovery_key ?? undefined,
    );
    await bridge.setDesktopGrant(grant);
    log("desktop identity authenticated");
  } catch (error) {
    log(`desktop identity authentication unavailable: ${errorName(error)}`);
  }
}

async function post<T>(path: string, body: object): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`identity endpoint returned ${response.status}`);
  return (await response.json()) as T;
}

function errorName(error: unknown): string {
  return error instanceof Error ? error.name : "unknown error";
}
