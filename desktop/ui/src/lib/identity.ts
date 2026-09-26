import type { Identity } from "../host";

export const SHARE_DOMAIN = "gnsis.studio";
export const shareLink = (id: Identity) => `https://${SHARE_DOMAIN}/${id.publicId.replace("gnsis:", "")}`;

const ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";

/**
 * Public ID from a public key: SHA-256 → base32 → first 12 characters,
 * grouped 4-4-4. Every host that creates an identity must derive the ID this
 * way, or the same key would show two different IDs in two places.
 */
export async function deriveId(publicKey: Uint8Array): Promise<string> {
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", publicKey as BufferSource));
  return `gnsis:${group(base32(digest, 12))}`;
}

export function base32(bytes: Uint8Array, length: number): string {
  let bits = 0;
  let value = 0;
  let out = "";
  for (const byte of bytes) {
    value = (value << 8) | byte;
    bits += 8;
    while (bits >= 5 && out.length < length) {
      out += ALPHABET[(value >>> (bits - 5)) & 31];
      bits -= 5;
    }
    if (out.length >= length) break;
  }
  return out;
}

const group = (s: string) => `${s.slice(0, 4)}-${s.slice(4, 8)}-${s.slice(8, 12)}`;
