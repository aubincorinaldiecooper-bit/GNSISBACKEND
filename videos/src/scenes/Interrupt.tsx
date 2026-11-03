import { interpolate, useCurrentFrame } from "remotion";
import { C, clamp, ease, MONO, ramp, Stage, Status, Voice } from "../ui/kit";

/**
 * Talking over it: GNSIS is mid-sentence, you cut in, it stops at once and
 * answers what you actually asked.
 */
export const INTERRUPT_FRAMES = 280;

const CUT_AT = 108;

function Words({ text, start, gap, stopAt, style }: { text: string; start: number; gap: number; stopAt?: number; style?: React.CSSProperties }) {
  const frame = useCurrentFrame();
  const shownUntil = stopAt === undefined ? frame : Math.min(frame, stopAt);
  return (
    <p style={{ margin: 0, ...style }}>
      {text.split(" ").map((w, i) => {
        const at = start + i * gap;
        const o = interpolate(shownUntil, [at, at + 4], [0, 1], { ...clamp, easing: ease });
        if (o === 0) return null;
        return (
          <span key={i} style={{ opacity: o, display: "inline-block", marginRight: "0.26em", translate: `0 ${(1 - o) * 10}px` }}>
            {w}
          </span>
        );
      })}
    </p>
  );
}

export function Interrupt() {
  const frame = useCurrentFrame();
  const cut = ramp(frame, CUT_AT, CUT_AT + 10);
  const firstOut = ramp(frame, 166, 180);
  const replyIn = frame >= 172;
  const label = (text: string, o: number) => (
    <div style={{ fontFamily: MONO, fontSize: 20, letterSpacing: "0.16em", textTransform: "uppercase", color: C.muted, marginBottom: 24, opacity: o }}>{text}</div>
  );

  return (
    <Stage>
      <Status
        steps={[
          { at: 0, text: "Speaking" },
          { at: CUT_AT, text: "Listening · stopped talking" },
          { at: 170, text: "Speaking" },
        ]}
      />

      {/* What GNSIS was saying when you cut in */}
      <div style={{ position: "absolute", left: 160, top: 300, width: 1560, opacity: 1 - firstOut, translate: `0 ${-firstOut * 40}px` }}>
        {label("GNSIS", 1 - cut * 0.6)}
        <Words
          text="I found three flights to Lisbon. One leaves at 9:40, one at 13:15, and the last one"
          start={6}
          gap={4}
          stopAt={CUT_AT}
          style={{ fontSize: 68, fontWeight: 300, letterSpacing: "-0.03em", lineHeight: 1.15, color: cut > 0 ? `rgba(245,245,244,${1 - 0.72 * cut})` : C.fg }}
        />
      </div>

      <Voice text="Just the morning one." start={CUT_AT + 6} end={160} y={800} />

      {/* The answer to what you asked */}
      {replyIn && (
        <div style={{ position: "absolute", left: 160, top: 300, width: 1560 }}>
          {label("GNSIS", ramp(frame, 172, 182))}
          <Words text="The 9:40. Opening it now." start={180} gap={5} style={{ fontSize: 96, fontWeight: 300, letterSpacing: "-0.04em", lineHeight: 1.05, color: C.fg }} />
        </div>
      )}
    </Stage>
  );
}
