/**
 * Two rules for GNSIS's window, set by the owner (30 September):
 *
 * - Hidden from screen capture. Screenshots, screen recordings and screen
 *   sharing (Zoom and the like) leave GNSIS out, and so does the screen
 *   picture GNSIS itself is sent, so it never looks at its own cards.
 * - Wherever the person is. Floating, GNSIS appears on every desktop (Space)
 *   and over full-screen apps, like the menu bar, instead of staying on the
 *   desktop it opened on.
 */
export interface RuledWindow {
  setContentProtection(enable: boolean): void;
  setVisibleOnAllWorkspaces(visible: boolean, options?: { visibleOnFullScreen?: boolean }): void;
}

/** Applies both rules; returns what was done, for the host log. */
export function applyWindowRules(win: RuledWindow, floating: boolean): string[] {
  win.setContentProtection(true);
  const done = ["hidden from screen capture"];
  if (floating) {
    win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
    done.push("on every desktop and over full-screen apps");
  }
  return done;
}
