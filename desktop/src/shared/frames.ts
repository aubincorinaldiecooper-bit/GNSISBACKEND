/**
 * Screen/camera sampling policy shared by renderer, transport, and tests.
 *
 * Pure functions only — the numbers mirror the production browser client
 * (runtime/minicpm_ft/mcpmft/infer/static/video.js) rather than inventing
 * a second visual policy.
 */

/** Max encoded frame dimension, matching the training contract in video.js. */
export const FRAME_FIT_PX = 448;
export const FRAME_JPEG_QUALITY = 0.7;

/**
 * Bounded transport-reconnect policy, mirroring video.js
 * RECONNECT_DELAYS_MS / RECONNECT_BUDGET_MS: reconnect under a still-active
 * capture session; never reacquire capture to repair transport.
 */
export const RECONNECT_DELAYS_MS = [500, 1000, 2000, 5000];
export const RECONNECT_BUDGET_MS = 60_000;

/** Scale `source` down so its largest side fits `fitPx`; never upscale. */
export function fitWithin(
  width: number,
  height: number,
  fitPx = FRAME_FIT_PX,
): { width: number; height: number } {
  const scale = Math.min(fitPx / width, fitPx / height, 1);
  return {
    width: Math.max(1, Math.round(width * scale)),
    height: Math.max(1, Math.round(height * scale)),
  };
}

/**
 * Honor the runtime's advertised `recommended_frame_rate` with guardrails:
 * non-positive/non-finite recommendations fall back to `fallbackHz`.
 */
export function resolveFrameRate(
  recommendedHz: unknown,
  fallbackHz = 1,
): number {
  const rate = Number(recommendedHz);
  return Number.isFinite(rate) && rate > 0 ? rate : fallbackHz;
}

/** Milliseconds to wait before reconnect attempt `n`, or null when the budget is spent. */
export function reconnectDelayMs(
  attempt: number,
  startedAtMs: number,
  nowMs = Date.now(),
): number | null {
  if (attempt < 0 || nowMs - startedAtMs > RECONNECT_BUDGET_MS) return null;
  return RECONNECT_DELAYS_MS[Math.min(attempt, RECONNECT_DELAYS_MS.length - 1)];
}
