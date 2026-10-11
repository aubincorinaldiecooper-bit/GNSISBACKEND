import { safeStorage } from "electron";
import {
  createHash,
  createPrivateKey,
  generateKeyPairSync,
  sign,
  type KeyObject,
} from "node:crypto";
import { existsSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";

interface StoredCredential {
  publicId: string;
  publicKey: string;
  encryptedPrivateKey: string;
}

interface ChallengeReply {
  challenge_id: string;
  challenge: string;
}

interface CompleteReply {
  grant: string;
}

export class ProtectedDesktopIdentity {
  constructor(
    private readonly userDataPath: string,
    private readonly apiUrl = "https://api.gnsis.studio",
  ) {}

  async authenticate(
    recoveryKey?: string,
    approvedByGrant?: string,
  ): Promise<string | null> {
    let credential = this.load();
    let created = false;
    if (!credential) {
      if (!recoveryKey) return null;
      credential = this.create();
      created = true;
    }
    const privateKey = this.privateKey(credential);
    const challenge = await this.post<ChallengeReply>(
      "/v1/desktop/identity/challenge",
      {
        public_id: credential.publicId,
        public_key: credential.publicKey,
      },
    );
    const signature = sign(
      "sha256",
      Buffer.from(challenge.challenge, "base64"),
      { key: privateKey, dsaEncoding: "ieee-p1363" },
    ).toString("base64");
    const completed = await this.post<CompleteReply>(
      "/v1/desktop/identity/complete",
      {
        challenge_id: challenge.challenge_id,
        signature,
        ...(recoveryKey ? { recovery_key: recoveryKey } : {}),
        ...(approvedByGrant
          ? { approved_by_grant: approvedByGrant }
          : {}),
      },
    );
    if (created) this.store(credential);
    return completed.grant;
  }

  storeRecoveryKey(recoveryKey: string): void {
    this.assertEncryption();
    writeFileSync(
      this.recoveryKeyPath(),
      safeStorage.encryptString(recoveryKey),
      { mode: 0o600 },
    );
  }

  erase(): void {
    rmSync(this.credentialPath(), { force: true });
    rmSync(this.recoveryKeyPath(), { force: true });
  }

  private create(): StoredCredential {
    this.assertEncryption();
    const { publicKey, privateKey } = generateKeyPairSync("ec", {
      namedCurve: "prime256v1",
    });
    const rawPublicKey = rawPoint(publicKey);
    const publicId = derivePublicId(rawPublicKey);
    const privatePem = privateKey.export({
      type: "pkcs8",
      format: "pem",
    }) as string;
    return {
      publicId,
      publicKey: rawPublicKey.toString("base64"),
      encryptedPrivateKey: safeStorage
        .encryptString(privatePem)
        .toString("base64"),
    };
  }

  private store(credential: StoredCredential): void {
    writeFileSync(this.credentialPath(), JSON.stringify(credential), {
      encoding: "utf8",
      mode: 0o600,
    });
  }

  private load(): StoredCredential | null {
    if (!existsSync(this.credentialPath())) return null;
    const parsed = JSON.parse(
      readFileSync(this.credentialPath(), "utf8"),
    ) as Partial<StoredCredential>;
    if (
      typeof parsed.publicId !== "string" ||
      typeof parsed.publicKey !== "string" ||
      typeof parsed.encryptedPrivateKey !== "string"
    ) {
      throw new Error("The protected desktop credential is invalid.");
    }
    return parsed as StoredCredential;
  }

  private privateKey(credential: StoredCredential): KeyObject {
    this.assertEncryption();
    const privatePem = safeStorage.decryptString(
      Buffer.from(credential.encryptedPrivateKey, "base64"),
    );
    return createPrivateKey(privatePem);
  }

  private assertEncryption(): void {
    if (!safeStorage.isEncryptionAvailable()) {
      throw new Error("System credential encryption is unavailable.");
    }
  }

  private credentialPath(): string {
    return path.join(this.userDataPath, "desktop-device-credential.json");
  }

  private recoveryKeyPath(): string {
    return path.join(this.userDataPath, "desktop-recovery-key");
  }

  private async post<T>(endpoint: string, body: object): Promise<T> {
    const response = await fetch(`${this.apiUrl}${endpoint}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      throw new Error(`Identity endpoint returned ${response.status}.`);
    }
    return (await response.json()) as T;
  }
}

function rawPoint(publicKey: KeyObject): Buffer {
  const jwk = publicKey.export({ format: "jwk" });
  if (typeof jwk.x !== "string" || typeof jwk.y !== "string") {
    throw new Error("The protected public key is invalid.");
  }
  return Buffer.concat([
    Buffer.from([4]),
    Buffer.from(jwk.x, "base64url"),
    Buffer.from(jwk.y, "base64url"),
  ]);
}

function derivePublicId(publicKey: Buffer): string {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  const digest = createHash("sha256").update(publicKey).digest();
  let bits = 0;
  let value = 0;
  let output = "";
  for (const byte of digest) {
    value = (value << 8) | byte;
    bits += 8;
    while (bits >= 5 && output.length < 12) {
      output += alphabet[(value >>> (bits - 5)) & 31];
      bits -= 5;
      value &= bits === 0 ? 0 : (1 << bits) - 1;
    }
    if (output.length >= 12) break;
  }
  return `gnsis:${output.slice(0, 4)}-${output.slice(4, 8)}-${output.slice(8, 12)}`;
}
