import { useEffect } from "react";
import type { LiveHost } from "../host";

/**
 * An overlay host covers the whole screen with a transparent window. Clicks
 * on empty areas must reach the apps underneath, so the UI reports where its
 * interactive surfaces are (every element marked `data-hit`) and the host
 * turns cursor pass-through on and off as the pointer moves. A host without
 * `reportHitRects` is an ordinary window and nothing is reported.
 */
export function useHitRects(host: LiveHost) {
  useEffect(() => {
    const report = host.reportHitRects?.bind(host);
    if (!report) return;
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
    return () => window.clearTimeout(timer);
  }, [host]);
}

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
