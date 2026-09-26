import type { Identity, IdentityStore } from "../host";
import { deriveId } from "../lib/identity";

/**
 * A device identity kept in the app's own storage.
 *
 * A key pair is made with WebCrypto and the private key is stored as a
 * non-extractable key in IndexedDB: it cannot be read out, exported or sent
 * anywhere by this code, and it persists across launches (Electron keeps
 * IndexedDB under the app's data folder). It is not the system keychain —
 * that is the device-key login work, still to come — and the wording in
 * Settings says so through `storageNote`.
 *
 * Nothing signs with this key yet. Its job today is to give this computer a
 * stable public ID and a face of its own.
 */
export class LocalIdentityStore implements IdentityStore {
  readonly storageNote =
    "Made on this computer and kept in GNSIS’s own storage here, where it cannot be read out or exported. Not the system keychain yet.";

  constructor(private readonly dbName = "gnsis-identity") {}

  async load(): Promise<Identity | null> {
    const record = await this.read();
    if (!record) return null;
    return { publicId: await deriveId(record.publicKey), publicKey: toBase64(record.publicKey), storage: "local" };
  }

  async create(): Promise<Identity> {
    const pair = await crypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, false, ["sign", "verify"]);
    const publicKey = new Uint8Array(await crypto.subtle.exportKey("raw", pair.publicKey));
    await this.write({ publicKey, privateKey: pair.privateKey });
    return { publicId: await deriveId(publicKey), publicKey: toBase64(publicKey), storage: "local" };
  }

  async erase(): Promise<void> {
    const db = await this.open();
    await request(db.transaction(STORE, "readwrite").objectStore(STORE).delete(KEY));
    db.close();
  }

  private async read(): Promise<Record | null> {
    const db = await this.open();
    const value = (await request(db.transaction(STORE, "readonly").objectStore(STORE).get(KEY))) as Record | undefined;
    db.close();
    return value && value.publicKey instanceof Uint8Array ? value : null;
  }

  private async write(record: Record): Promise<void> {
    const db = await this.open();
    await request(db.transaction(STORE, "readwrite").objectStore(STORE).put(record, KEY));
    db.close();
  }

  private open(): Promise<IDBDatabase> {
    if (typeof indexedDB === "undefined") {
      return Promise.reject(new Error("This computer’s browser storage is not available, so a key cannot be kept."));
    }
    return new Promise((resolve, reject) => {
      const req = indexedDB.open(this.dbName, 1);
      req.onupgradeneeded = () => {
        if (!req.result.objectStoreNames.contains(STORE)) req.result.createObjectStore(STORE);
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error ?? new Error("The key store could not be opened."));
      req.onblocked = () => reject(new Error("The key store is in use by another window."));
    });
  }
}

interface Record {
  publicKey: Uint8Array;
  privateKey: CryptoKey;
}

const STORE = "identity";
const KEY = "device";

function request<T>(req: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error ?? new Error("The key store failed."));
  });
}

function toBase64(bytes: Uint8Array): string {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s);
}
