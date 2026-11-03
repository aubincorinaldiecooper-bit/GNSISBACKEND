import "@fontsource/geist-sans/300.css";
import "@fontsource/geist-sans/400.css";
import "@fontsource/geist-sans/500.css";
import "@fontsource/geist-sans/600.css";
import "@fontsource/geist-mono/400.css";
import { useState, type CSSProperties, type ReactNode } from "react";
import {
  AbsoluteFill,
  continueRender,
  delayRender,
  Easing,
  interpolate,
  Sequence,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { WordStream } from "../remocn/word-stream";

/** Same palette and faces as the landing page. */
export const C = {
  bg: "#000000",
  fg: "#f5f5f4",
  muted: "#8a8a87",
  dim: "#4a4a48",
  line: "rgba(255,255,255,0.12)",
  panel: "#0a0a0a",
  hi: "rgba(255,255,255,0.08)",
};
export const SANS = "'Geist Sans', -apple-system, 'Segoe UI', sans-serif";
export const MONO = "'Geist Mono', ui-monospace, Menlo, monospace";

const clamp = { extrapolateLeft: "clamp", extrapolateRight: "clamp" } as const;
export const ease = Easing.bezier(0.2, 0.8, 0.2, 1);

/** 0 → 1 between two frames, eased. */
export function ramp(frame: number, from: number, to: number) {
  return interpolate(frame, [from, to], [0, 1], { ...clamp, easing: ease });
}

/** Holds the render until the web fonts are ready, so no frame uses a fallback face. */
export function Stage({ children }: { children: ReactNode }) {
  const [handle] = useState(() => delayRender("fonts"));
  useState(() => {
    document.fonts.ready.then(() => continueRender(handle));
    return null;
  });
  const frame = useCurrentFrame();
  const { durationInFrames } = useVideoConfig();
  // Fade in and out so the clip loops cleanly on the page.
  const opacity = Math.min(ramp(frame, 0, 12), 1 - ramp(frame, durationInFrames - 14, durationInFrames - 1));
  return (
    <AbsoluteFill style={{ background: C.bg, color: C.fg, fontFamily: SANS }}>
      <AbsoluteFill style={{ opacity }}>{children}</AbsoluteFill>
    </AbsoluteFill>
  );
}

/** The small mono line in the top-left that says what GNSIS is doing. */
export function Status({ steps }: { steps: Array<{ at: number; text: string }> }) {
  const frame = useCurrentFrame();
  let current = steps[0];
  for (const s of steps) if (frame >= s.at) current = s;
  const since = frame - current.at;
  const pulse = 0.35 + 0.65 * Math.abs(Math.sin(frame / 9));
  return (
    <div style={{ position: "absolute", left: 96, top: 84, display: "flex", alignItems: "center", gap: 16, fontFamily: MONO, fontSize: 22, letterSpacing: "0.16em", textTransform: "uppercase", color: C.muted }}>
      <span style={{ width: 10, height: 10, borderRadius: 5, background: C.fg, opacity: pulse }} />
      <span style={{ opacity: ramp(since, 0, 8), translate: `0 ${(1 - ramp(since, 0, 8)) * 8}px` }}>{current.text}</span>
    </div>
  );
}

/** What the person says: a small label, a live waveform and the words appearing as spoken. */
export function Voice({ text, who = "You", start, end, y = 900 }: { text: string; who?: string; start: number; end: number; y?: number }) {
  const frame = useCurrentFrame();
  if (frame < start - 4 || frame > end + 12) return null;
  const local = frame - start;
  const show = Math.min(ramp(local, -4, 4), 1 - ramp(frame, end, end + 12));
  const talking = frame >= start && frame <= end - 10;
  return (
    <div style={{ position: "absolute", left: 0, right: 0, top: y - 70, height: 170, opacity: show }}>
      <div style={{ position: "absolute", left: 0, right: 0, top: 0, display: "flex", justifyContent: "center", alignItems: "center", gap: 14, fontFamily: MONO, fontSize: 20, letterSpacing: "0.16em", textTransform: "uppercase", color: C.muted }}>
        {who}
        <span style={{ display: "inline-flex", alignItems: "center", gap: 4, height: 22 }}>
          {[0, 1, 2, 3, 4, 5, 6].map((i) => {
            const h = talking ? 6 + 16 * Math.abs(Math.sin(frame / 3.2 + i * 1.3)) : 4;
            return <span key={i} style={{ width: 3, height: h, borderRadius: 2, background: C.fg }} />;
          })}
        </span>
      </div>
      <div style={{ position: "absolute", left: 0, right: 0, top: 50, height: 120 }}>
        <Delay by={start}>
          <WordStream text={text} fontSize={56} color={C.fg} fontWeight={300} wordGap={5} hold={30} />
        </Delay>
      </div>
    </div>
  );
}

/** Shifts a child's clock so components that count from frame 0 can start later. */
export function Delay({ by, children }: { by: number; children: ReactNode }) {
  return (
    <Sequence from={by} layout="none">
      {children}
    </Sequence>
  );
}

/** A plain Mac-style window drawn in outline. */
export function Window({ title, x, y, w, h, appear, children }: { title: string; x: number; y: number; w: number; h: number; appear: number; children?: ReactNode }) {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const s = spring({ frame: frame - appear, fps, config: { damping: 200 } });
  return (
    <div style={{ position: "absolute", left: x, top: y, width: w, height: h, borderRadius: 18, border: `1px solid ${C.line}`, background: C.panel, overflow: "hidden", opacity: s, scale: `${0.97 + 0.03 * s}`, translate: `0 ${(1 - s) * 24}px` }}>
      <div style={{ height: 52, borderBottom: `1px solid ${C.line}`, display: "flex", alignItems: "center", padding: "0 20px", gap: 8 }}>
        {[0, 1, 2].map((i) => (
          <span key={i} style={{ width: 12, height: 12, borderRadius: 6, background: "rgba(255,255,255,0.18)" }} />
        ))}
        <span style={{ flex: 1, textAlign: "center", fontSize: 20, color: C.muted, marginRight: 52 }}>{title}</span>
      </div>
      <div style={{ position: "absolute", inset: "52px 0 0 0" }}>{children}</div>
    </div>
  );
}

/** Text that types itself out, one character every `perChar` frames, with a caret while typing. */
export function Typed({ text, start, perChar = 1.6, style }: { text: string; start: number; perChar?: number; style?: CSSProperties }) {
  const frame = useCurrentFrame();
  const n = Math.max(0, Math.min(text.length, Math.floor((frame - start) / perChar)));
  const typing = frame >= start && n < text.length;
  const caretOn = frame >= start && (typing || Math.floor(frame / 15) % 2 === 0) && frame < start + text.length * perChar + 30;
  return (
    <span style={{ whiteSpace: "pre-wrap", ...style }}>
      {text.slice(0, n)}
      <span style={{ display: "inline-block", width: "0.06em", minWidth: 2, height: "1em", marginLeft: 2, verticalAlign: "-0.12em", background: C.fg, opacity: caretOn ? 1 : 0 }} />
    </span>
  );
}

export { clamp };
