import { useEffect } from "react";
import type { LiveHost } from "../host";

/**
 * An overlay host covers the screen with a transparent window. Clicks on
 * empty areas must reach the apps underneath, so the UI reports where its
 * interactive surfaces are (every element marked `data-hit`) and the host
 * turns cursor pass-through on and off as the pointer moves. Only an overlay
 * host is sent them: an ordinary window takes every click anyway.
 */
export function useHitRects(host: LiveHost) {
  useEffect(() => {
    const report = host.reportHitRects?.bind(host);
    if (!report || !host.capabilities().overlay) return;
    let last = "";
    let timer = 0;
    const tick = () => {
      const rects = Array.from(document.querySelectorAll<HTMLElement>("[data-hit]")).map((el) => {
        const r = el.getBoundingClientRect();
        return [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)] as [
          number,
          number,
          number,
          number,
        ];
      });
      const key = JSON.stringify(rects);
      if (key !== last) {
        last = key;
        report(rects);
      }
      timer = window.setTimeout(tick, 120);
    };
    tick();
    // Something just moved under the pointer (GNSIS being dragged): report now, not on the next beat.
    const now = () => {
      window.clearTimeout(timer);
      tick();
    };
    window.addEventListener(HIT_RECTS_NOW, now);
    return () => {
      window.clearTimeout(timer);
      window.removeEventListener(HIT_RECTS_NOW, now);
    };
  }, [host]);
}

/** Ask the overlay host to take the interactive surfaces' new places at once. */
export const HIT_RECTS_NOW = "gnsis:hit-rects-now";

/**
 * The wake gesture. The hardware is not specified yet, so ⌥Space stands in
 * while the window has focus. It toggles live voice with whoever is in front.
 */
export function useGesture(onGesture: () => void) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.altKey && e.code === "Space") {
        e.preventDefault();
        onGesture();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onGesture]);
}

export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

/** m:ss for a duration in milliseconds. */
export function clock(ms: number): string {
  const secs = Math.max(0, Math.round(ms / 1000));
  return `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, "0")}`;
}
