/**
 * Electron implementations of the main-process adapters: permissions,
 * global shortcuts, notifications.
 * The only file that may import electron for these capabilities.
 */
import {
  globalShortcut,
  Notification,
  systemPreferences,
} from "electron";
import type {
  DesktopPermissionAdapter,
  NotificationAdapter,
  PermissionKind,
  PermissionState,
  ShortcutAdapter,
} from "./adapters.js";

export class ElectronPermissions implements DesktopPermissionAdapter {
  async status(kind: PermissionKind): Promise<PermissionState> {
    if (process.platform !== "darwin") return "unknown";
    const media =
      kind === "screen" ? "screen" : kind === "microphone" ? "microphone" : "camera";
    return systemPreferences.getMediaAccessStatus(media) as PermissionState;
  }

  async request(kind: PermissionKind): Promise<PermissionState> {
    if (process.platform !== "darwin") return "unknown";
    if (kind === "screen") {
      // macOS has no programmatic screen-recording request; read status and
      // let the OS prompt on first capture attempt.
      return this.status(kind);
    }
    const ok = await systemPreferences.askForMediaAccess(
      kind === "microphone" ? "microphone" : "camera",
    );
    return ok ? "granted" : "denied";
  }
}

export class ElectronShortcuts implements ShortcutAdapter {
  register(accelerator: string, cb: () => void): void {
    globalShortcut.register(accelerator, cb);
  }

  unregisterAll(): void {
    globalShortcut.unregisterAll();
  }
}

export class ElectronNotifications implements NotificationAdapter {
  show(title: string, body: string): void {
    new Notification({ title, body }).show();
  }
}
