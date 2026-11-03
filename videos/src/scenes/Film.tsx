import type { CSSProperties, ReactNode } from "react";
import { Img, interpolate, spring, staticFile, useCurrentFrame, useVideoConfig } from "remotion";
import { C, clamp, ease, MONO, ramp, SANS, Stage } from "../ui/kit";

/**
 * The 30-second brand film: from typing and clicking to just saying it,
 * then what GNSIS is, beat by beat, ending on the wordmark.
 */
export const FILM_FRAMES = 900;

/** One beat: blurs and settles in, holds, then blurs out. Children get the beat's local frame. */
function Beat({ from, to, children, style }: { from: number; to: number; children: (local: number) => ReactNode; style?: CSSProperties }) {
  const frame = useCurrentFrame();
  if (frame < from || frame > to) return null;
  const local = frame - from;
  const inn = ramp(frame, from, from + 18);
  const out = ramp(frame, to - 14, to);
  const o = inn * (1 - out);
  return (
    <div style={{ position: "absolute", inset: 0, opacity: o, filter: `blur(${(1 - inn) * 14 + out * 10}px)`, scale: `${1.035 - 0.035 * inn + out * 0.02}`, ...style }}>
      {children(local)}
    </div>
  );
}

const headline = (size: number, extra?: CSSProperties): CSSProperties => ({
  fontFamily: SANS,
  fontSize: size,
  fontWeight: 600,
  letterSpacing: "-0.045em",
  lineHeight: 1.02,
  color: C.fg,
  textAlign: "center",
  margin: 0,
  ...extra,
});

const center: CSSProperties = { position: "absolute", inset: 0, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center" };

/** Words that rise in one by one. */
function Rise({ text, at, gap = 4, style }: { text: string; at: number; gap?: number; style?: CSSProperties }) {
  const frame = useCurrentFrame();
  return (
    <p style={style}>
      {text.split(" ").map((w, i) => {
        const o = interpolate(frame, [at + i * gap, at + i * gap + 14], [0, 1], { ...clamp, easing: ease });
        return (
          <span key={i} style={{ display: "inline-block", opacity: o, translate: `0 ${(1 - o) * 0.35}em`, marginRight: "0.24em" }}>
            {w}
          </span>
        );
      })}
    </p>
  );
}

function Orb({ size, spin = 0 }: { size: number; spin?: number }) {
  const frame = useCurrentFrame();
  const breathe = 1 + 0.03 * Math.sin(frame / 22);
  return (
    <div style={{ width: size, height: size, position: "relative" }}>
      <Img src={staticFile("omni.webp")} style={{ position: "absolute", inset: 0, width: "100%", height: "100%", rotate: `${frame * 0.12 + spin}deg`, scale: `${breathe}` }} />
      <Img src={staticFile("omni.webp")} style={{ position: "absolute", inset: 0, width: "100%", height: "100%", opacity: 0.35, mixBlendMode: "screen", rotate: `${-frame * 0.08}deg`, scale: `${1.06 - (breathe - 1)}` }} />
    </div>
  );
}

/* ---------- small drawings for each feature beat ---------- */

function SeesArt({ t }: { t: number }) {
  const k = ramp(t, 6, 30);
  const bx = interpolate(k, [0, 1], [0, 300]);
  const by = interpolate(k, [0, 1], [0, 150]);
  const bw = interpolate(k, [0, 1], [640, 180]);
  const bh = interpolate(k, [0, 1], [400, 64]);
  const corner = (s: CSSProperties) => <span style={{ position: "absolute", width: 22, height: 22, borderColor: C.fg, borderStyle: "solid", borderWidth: 0, ...s }} />;
  return (
    <div style={{ position: "relative", width: 640, height: 400, borderRadius: 22, border: `1.5px solid ${C.line}` }}>
      {[0, 1, 2].map((i) => (
        <span key={i} style={{ position: "absolute", left: 40, top: 60 + i * 70, width: 220 - i * 50, height: 14, borderRadius: 7, background: "rgba(255,255,255,0.1)" }} />
      ))}
      <span style={{ position: "absolute", left: 300, top: 150, width: 180, height: 64, borderRadius: 14, background: "rgba(255,255,255,0.12)" }} />
      <div style={{ position: "absolute", left: bx - 10, top: by - 10, width: bw + 20, height: bh + 20 }}>
        {corner({ left: 0, top: 0, borderLeftWidth: 3, borderTopWidth: 3, borderTopLeftRadius: 10 })}
        {corner({ right: 0, top: 0, borderRightWidth: 3, borderTopWidth: 3, borderTopRightRadius: 10 })}
        {corner({ left: 0, bottom: 0, borderLeftWidth: 3, borderBottomWidth: 3, borderBottomLeftRadius: 10 })}
        {corner({ right: 0, bottom: 0, borderRightWidth: 3, borderBottomWidth: 3, borderBottomRightRadius: 10 })}
      </div>
    </div>
  );
}

function HearsArt({ t }: { t: number }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 12, height: 260 }}>
      {Array.from({ length: 23 }).map((_, i) => {
        const env = Math.sin((i / 22) * Math.PI);
        const h = 18 + env * 200 * (0.35 + 0.65 * Math.abs(Math.sin(t / 5 + i * 0.7)));
        return <span key={i} style={{ width: 12, height: h, borderRadius: 6, background: C.fg, opacity: 0.4 + 0.6 * env }} />;
      })}
    </div>
  );
}

function ActsArt({ t }: { t: number }) {
  const { fps } = useVideoConfig();
  const move = ramp(t, 4, 30);
  const press = spring({ frame: t - 32, fps, config: { damping: 12, stiffness: 220 } });
  const x = interpolate(move, [0, 1], [560, 330]);
  const y = interpolate(move, [0, 1], [380, 200]);
  const pressed = t >= 32 && t < 44;
  return (
    <div style={{ position: "relative", width: 640, height: 400 }}>
      <div style={{ position: "absolute", left: 190, top: 156, width: 260, height: 88, borderRadius: 44, background: pressed ? "#d6d6d4" : C.fg, color: C.bg, display: "grid", placeItems: "center", fontFamily: SANS, fontSize: 34, fontWeight: 500, scale: pressed ? "0.96" : "1" }}>
        New Note
      </div>
      {t >= 32 && <span style={{ position: "absolute", left: 330 - 60 * press, top: 200 - 60 * press, width: 120 * press, height: 120 * press, borderRadius: "50%", border: `2px solid ${C.fg}`, opacity: 1 - press }} />}
      <svg width="54" height="54" viewBox="0 0 24 24" style={{ position: "absolute", left: x, top: y }}>
        <path d="M5 3L5 19L9.5 14.5L12.5 21L15 20L12 13.5L18.5 13.5L5 3Z" fill={C.fg} stroke="#000" strokeWidth="1.2" strokeLinejoin="round" />
      </svg>
    </div>
  );
}

function AsksArt({ t }: { t: number }) {
  const { fps } = useVideoConfig();
  const s = spring({ frame: t - 4, fps, config: { damping: 16, stiffness: 120 } });
  return (
    <div style={{ width: 640, borderRadius: 28, border: "1.5px solid rgba(255,255,255,0.28)", background: "#0b0b0b", padding: 40, translate: `0 ${(1 - s) * 50}px`, opacity: s }}>
      <div style={{ fontFamily: SANS, fontSize: 40, fontWeight: 500, letterSpacing: "-0.02em", color: C.fg }}>Move 3 files to the Trash?</div>
      <div style={{ marginTop: 12, fontFamily: MONO, fontSize: 20, color: C.muted }}>draft-1.key, draft-2.key, draft-3.key</div>
      <div style={{ display: "flex", gap: 16, marginTop: 36 }}>
        <div style={{ flex: 1, height: 64, borderRadius: 16, background: C.fg, color: C.bg, display: "grid", placeItems: "center", fontFamily: SANS, fontSize: 26, fontWeight: 500 }}>Move</div>
        <div style={{ flex: 1, height: 64, borderRadius: 16, border: `1.5px solid ${C.line}`, color: C.muted, display: "grid", placeItems: "center", fontFamily: SANS, fontSize: 26 }}>Not now</div>
      </div>
    </div>
  );
}

const NODES = [
  [80, 260], [210, 110], [330, 300], [470, 150], [580, 290], [300, 40], [520, 30],
] as const;
const LINKS = [[0, 1], [1, 2], [1, 5], [2, 3], [3, 4], [3, 6], [5, 6], [2, 4]] as const;

function RemembersArt({ t }: { t: number }) {
  return (
    <svg width="660" height="340" viewBox="0 0 660 340" style={{ overflow: "visible" }}>
      {LINKS.map(([a, b], i) => {
        const k = ramp(t, 6 + i * 4, 20 + i * 4);
        const [x1, y1] = NODES[a];
        const [x2, y2] = NODES[b];
        return <line key={i} x1={x1} y1={y1} x2={x1 + (x2 - x1) * k} y2={y1 + (y2 - y1) * k} stroke={C.fg} strokeOpacity={0.55} strokeWidth={2} />;
      })}
      {NODES.map(([x, y], i) => {
        const k = ramp(t, i * 4, i * 4 + 12);
        return <circle key={i} cx={x} cy={y} r={11 * k} fill={C.fg} />;
      })}
    </svg>
  );
}

const FEATURES: Array<{ title: string; Art: (p: { t: number }) => ReactNode }> = [
  { title: "It sees your screen.", Art: SeesArt },
  { title: "It hears you.", Art: HearsArt },
  { title: "It uses your apps.", Art: ActsArt },
  { title: "It asks first.", Art: AsksArt },
  { title: "It remembers.", Art: RemembersArt },
];
const FEATURE_START = 330;
const FEATURE_LEN = 72;

/** Glyphs for the "how we used to do it" montage. */
function Glyph({ kind }: { kind: "type" | "click" | "tap" }) {
  if (kind === "type")
    return <div style={{ fontFamily: MONO, fontSize: 150, color: C.fg, letterSpacing: "-0.04em" }}>&gt;_</div>;
  if (kind === "click")
    return (
      <svg width="150" height="150" viewBox="0 0 24 24">
        <path d="M5 3L5 19L9.5 14.5L12.5 21L15 20L12 13.5L18.5 13.5L5 3Z" fill={C.fg} />
      </svg>
    );
  return (
    <svg width="170" height="170" viewBox="0 0 100 100">
      <circle cx="50" cy="50" r="16" fill={C.fg} />
      <circle cx="50" cy="50" r="32" fill="none" stroke={C.fg} strokeOpacity="0.5" strokeWidth="2" />
      <circle cx="50" cy="50" r="46" fill="none" stroke={C.fg} strokeOpacity="0.25" strokeWidth="2" />
    </svg>
  );
}

export function Film() {
  const frame = useCurrentFrame();

  return (
    <Stage>
      {/* 1 — the past */}
      <Beat from={0} to={80}>
        {() => (
          <div style={center}>
            <Rise text="For fifty years," at={10} style={headline(120)} />
          </div>
        )}
      </Beat>

      <Beat from={76} to={168}>
        {(t) => {
          const kind = t < 30 ? "type" : t < 58 ? "click" : "tap";
          const word = kind === "type" ? "Type." : kind === "click" ? "Click." : "Tap.";
          const step = t < 30 ? t : t < 58 ? t - 30 : t - 58;
          const pop = ramp(step, 0, 10);
          return (
            <div style={{ ...center, gap: 56 }}>
              <div style={{ height: 180, display: "grid", placeItems: "center", opacity: pop, scale: `${0.92 + 0.08 * pop}` }}>
                <Glyph kind={kind} />
              </div>
              <p style={headline(64, { fontWeight: 400, color: C.muted, letterSpacing: "-0.03em" })}>
                we learned to <span style={{ color: C.fg }}>{word.toLowerCase().replace(".", "")}</span>.
              </p>
            </div>
          );
        }}
      </Beat>

      {/* 2 — the turn */}
      <Beat from={164} to={238}>
        {() => (
          <div style={center}>
            <Rise text="Now, just say it." at={176} gap={5} style={headline(140)} />
          </div>
        )}
      </Beat>

      {/* 3 — meet it */}
      <Beat from={234} to={334}>
        {(t) => {
          const grow = ramp(t, 0, 60);
          const word = ramp(t, 34, 58);
          return (
            <div style={center}>
              <div style={{ position: "absolute", opacity: 0.55 + 0.45 * grow, scale: `${0.7 + 0.35 * grow}` }}>
                <Orb size={820} />
              </div>
              <p style={headline(200, { position: "relative", opacity: word, letterSpacing: `${0.4 - 0.28 * word}em`, fontWeight: 500, paddingLeft: `${0.4 - 0.28 * word}em`, textShadow: "0 0 60px rgba(0,0,0,0.9)" })}>GNSIS</p>
            </div>
          );
        }}
      </Beat>

      {/* 4 — what it does */}
      {FEATURES.map(({ title, Art }, i) => {
        const from = FEATURE_START + i * FEATURE_LEN;
        return (
          <Beat key={title} from={from} to={from + FEATURE_LEN + 4}>
            {(t) => (
              <div style={{ ...center, gap: 64 }}>
                <div style={{ height: 420, display: "grid", placeItems: "center" }}>
                  <Art t={t} />
                </div>
                <Rise text={title} at={from + 6} gap={3} style={headline(92)} />
              </div>
            )}
          </Beat>
        );
      })}

      {/* 5 — where it lives */}
      <Beat from={FEATURE_START + FEATURES.length * FEATURE_LEN} to={830}>
        {(t) => {
          const lid = ramp(t, 0, 30);
          return (
            <div style={{ ...center, gap: 48 }}>
              <div style={{ position: "relative", width: 760, height: 470 }}>
                <div style={{ position: "absolute", left: 60, top: 0, width: 640, height: 410, borderRadius: 26, border: `2px solid ${C.line}`, overflow: "hidden", display: "grid", placeItems: "center", opacity: lid }}>
                  <div style={{ scale: `${0.8 + 0.2 * lid}` }}>
                    <Orb size={380} spin={40} />
                  </div>
                </div>
                <div style={{ position: "absolute", left: 0, bottom: 0, width: 760, height: 26, borderRadius: "4px 4px 22px 22px", border: `2px solid ${C.line}`, borderTopColor: "rgba(255,255,255,0.22)" }} />
              </div>
              <div>
                <Rise text="An omni agent," at={FEATURE_START + FEATURES.length * FEATURE_LEN + 10} gap={4} style={headline(84)} />
                <Rise text="on your device." at={FEATURE_START + FEATURES.length * FEATURE_LEN + 34} gap={4} style={headline(84, { color: C.muted })} />
              </div>
            </div>
          );
        }}
      </Beat>

      {/* 6 — sign-off */}
      <Beat from={826} to={FILM_FRAMES + 20}>
        {(t) => (
          <div style={{ ...center, gap: 28 }}>
            <p style={headline(180, { fontWeight: 500, letterSpacing: "0.12em", paddingLeft: "0.12em" })}>GNSIS</p>
            <p style={{ margin: 0, fontFamily: MONO, fontSize: 26, letterSpacing: "0.3em", textTransform: "uppercase", color: C.muted, opacity: ramp(t, 14, 30) }}>Coming to Mac</p>
          </div>
        )}
      </Beat>

      {/* a hairline progress mark, like a keynote reel */}
      <div style={{ position: "absolute", left: 0, bottom: 0, height: 2, width: `${(frame / FILM_FRAMES) * 100}%`, background: "rgba(255,255,255,0.12)" }} />
    </Stage>
  );
}
