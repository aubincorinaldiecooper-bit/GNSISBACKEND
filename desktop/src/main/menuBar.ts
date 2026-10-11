/**
 * GNSIS's icon in the Mac menu bar (the owner's call, 30 September): GNSIS
 * tucks itself into it and comes back from it. Clicking the icon, or "Hide to
 * menu bar" in GNSIS's ≡ menu, hides the floating window; clicking the icon
 * again shows it where it was.
 *
 * The icon is the person's own GNSIS face in one colour. The page knows the
 * face, draws it and hands it over as a PNG; the icon appears only then, so
 * there is no icon before there is a GNSIS on the desktop.
 *
 * The page does the tucking itself (ending a call, shrinking into the icon)
 * and then asks for the window to hide, so the window never vanishes
 * half-way. If the page does not answer, the window hides anyway.
 *
 * A right-click on the icon opens a small menu with "Quit GNSIS" (the
 * owner's call, 30 September): floating over full-screen apps leaves GNSIS
 * without the Dock and app menu a Mac app is normally quit from.
 */

export interface TrayLike {
  setImage(image: unknown): void;
  setToolTip(text: string): void;
  getBounds(): { x: number; y: number; width: number; height: number };
  on(event: "click", listener: () => void): unknown;
  on(event: "right-click", listener: () => void): unknown;
  popUpContextMenu(menu?: unknown): void;
  destroy(): void;
}

export interface WindowLike {
  isDestroyed(): boolean;
  isVisible(): boolean;
  hide(): void;
  showInactive(): void;
  getBounds(): { x: number; y: number; width: number; height: number };
}

/** What the page is told: to tuck away or come back (`want`), and where the icon is (`at`, page pixels from the window's top left). */
export interface MenuBarMessage {
  want?: "hide" | "show";
  at?: { x: number; y: number };
}

export interface MenuBarOptions {
  makeTray(image: unknown): TrayLike;
  /** A PNG data URL drawn at twice the icon's size, as a macOS template image. */
  image(png: string): unknown;
  window(): WindowLike | null;
  send(message: MenuBarMessage): void;
  /** The page's zoom: the page measures in its own pixels. */
  zoom: number;
  log(line: string): void;
  /** How long the page has to tuck away before the window hides regardless. */
  hideAfterMs?: number;
  /** GNSIS's window was closed while the icon stayed: open it again. */
  reopen?(): void;
  /**
   * The page did not tuck away in time, so it did not end the call either:
   * end it from here before the window hides, so nothing keeps listening
   * behind a hidden GNSIS.
   */
  endCall?(): void;
  /** The menu a right-click on the icon opens ("Quit GNSIS"). Built by main, which has Electron's Menu. */
  rightClickMenu?(): unknown;
  setTimeout?: (fn: () => void, ms: number) => unknown;
  clearTimeout?: (handle: unknown) => void;
}

/** A face larger than this is not an icon (a 36-pixel PNG is a few kilobytes). */
const MAX_FACE_CHARS = 200_000;

export class MenuBar {
  private tray: TrayLike | null = null;
  /** Asked the page to tuck away; the window hides when it has, or when this fires. */
  private hiding: unknown = null;
  private readonly setT: (fn: () => void, ms: number) => unknown;
  private readonly clearT: (handle: unknown) => void;

  constructor(private readonly o: MenuBarOptions) {
    this.setT = o.setTimeout ?? ((fn, ms) => setTimeout(fn, ms));
    this.clearT = o.clearTimeout ?? ((h) => clearTimeout(h as ReturnType<typeof setTimeout>));
  }

  /** The page's drawing of the face: the icon appears, or changes to it. */
  setFace(png: unknown): void {
    if (typeof png !== "string" || !png.startsWith("data:image/png;base64,") || png.length > MAX_FACE_CHARS) {
      this.o.log("menu bar: ignored a face that is not a small PNG");
      return;
    }
    const image = this.o.image(png);
    if (this.tray) {
      this.tray.setImage(image);
    } else {
      this.tray = this.o.makeTray(image);
      this.tray.setToolTip("GNSIS");
      this.tray.on("click", () => this.click());
      this.tray.on("right-click", () => this.rightClick());
      this.o.log("menu bar: icon shown");
    }
    const at = this.iconAt();
    if (at) this.o.send({ at });
  }

  /** Where the icon is, from the window's top left, in page pixels: above the window, so y is negative. */
  iconAt(): { x: number; y: number } | null {
    const w = this.o.window();
    if (!this.tray || !w || w.isDestroyed()) return null;
    const b = this.tray.getBounds();
    if (!b.width || !b.height) return null;
    const wb = w.getBounds();
    return { x: Math.round((b.x + b.width / 2 - wb.x) / this.o.zoom), y: Math.round((b.y + b.height / 2 - wb.y) / this.o.zoom) };
  }

  /** The icon was clicked: tuck away if GNSIS is out, come back if it is tucked away or on its way. */
  click(): void {
    const w = this.o.window();
    if (!w || w.isDestroyed()) return this.reopen();
    if (w.isVisible() && this.hiding === null) {
      this.hiding = this.setT(() => {
        this.o.log("menu bar: the page did not tuck away in time; ending any call and hiding anyway");
        this.o.endCall?.();
        this.hideNow();
      }, this.o.hideAfterMs ?? 1_000);
      this.o.send({ want: "hide", at: this.iconAt() ?? undefined });
    } else {
      this.show();
    }
  }

  /** A right-click on the icon: its small menu, with "Quit GNSIS". */
  rightClick(): void {
    const menu = this.o.rightClickMenu?.();
    if (this.tray && menu) this.tray.popUpContextMenu(menu);
  }

  /** The page has tucked GNSIS away (after a click on the icon, or from its own menu): hide the window. */
  pageTuckedAway(): void {
    this.hideNow();
  }

  /** Bring GNSIS back if it is tucked away (the icon, or the app's Dock icon). */
  show(): void {
    this.stopHiding();
    const w = this.o.window();
    if (!w || w.isDestroyed()) return this.reopen();
    if (!w.isVisible()) w.showInactive();
    this.o.send({ want: "show", at: this.iconAt() ?? undefined });
  }

  /** Whether GNSIS is tucked away: the icon exists and the window is hidden. */
  get tucked(): boolean {
    const w = this.o.window();
    return !!this.tray && !!w && !w.isDestroyed() && !w.isVisible();
  }

  destroy(): void {
    this.stopHiding();
    this.tray?.destroy();
    this.tray = null;
  }

  private reopen(): void {
    this.stopHiding();
    this.o.log("menu bar: GNSIS's window was closed; opening it again");
    this.o.reopen?.();
  }

  private hideNow(): void {
    this.stopHiding();
    const w = this.o.window();
    if (w && !w.isDestroyed() && w.isVisible()) w.hide();
  }

  private stopHiding(): void {
    if (this.hiding !== null) this.clearT(this.hiding);
    this.hiding = null;
  }
}
