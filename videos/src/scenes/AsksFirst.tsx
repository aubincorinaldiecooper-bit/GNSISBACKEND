import { spring, useCurrentFrame, useVideoConfig } from "remotion";
import { SimulatedCursor } from "../remocn/simulated-cursor";
import { C, Delay, MONO, ramp, Stage, Status, Voice, Window } from "../ui/kit";

/**
 * "Clear out my old drafts": GNSIS finds them, then stops and asks before
 * anything you can't take back. You say yes; it moves them.
 */
export const ASKS_FIRST_FRAMES = 300;

const WIN = { x: 460, y: 190, w: 1000, h: 600 };
const FILES = [
  { name: "Invoice-October.pdf", meta: "PDF · 214 KB", draft: false },
  { name: "draft-1.key", meta: "Keynote · 3.1 MB", draft: true },
  { name: "Pitch-final.key", meta: "Keynote · 8.4 MB", draft: false },
  { name: "draft-2.key", meta: "Keynote · 2.7 MB", draft: true },
  { name: "draft-3.key", meta: "Keynote · 2.9 MB", draft: true },
];
const CARD = { x: 660, y: 610, w: 600, h: 230 };
const MOVE_BTN = { x: CARD.x + 32 + 120, y: CARD.y + 150 + 28 };

export function AsksFirst() {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const cardIn = spring({ frame: frame - 120, fps, config: { damping: 18, stiffness: 140 } });
  const cardOut = ramp(frame, 192, 204);
  const card = cardIn * (1 - cardOut);
  const gone = ramp(frame, 200, 222);
  let nthDraft = 0;

  return (
    <Stage>
      <Status
        steps={[
          { at: 0, text: "Listening" },
          { at: 64, text: "Looking · Finder" },
          { at: 118, text: "Asking first" },
          { at: 186, text: "Acting" },
          { at: 226, text: "Done · 3 files in the Trash" },
        ]}
      />
      <Voice text="Clear out my old drafts." start={8} end={58} y={520} />

      <Window title="Documents" appear={66} {...WIN}>
        <div style={{ padding: "16px 28px", display: "flex", justifyContent: "space-between", fontFamily: MONO, fontSize: 16, letterSpacing: "0.12em", textTransform: "uppercase", color: C.dim, borderBottom: `1px solid ${C.line}` }}>
          <span>Name</span>
          <span>Kind</span>
        </div>
        {FILES.map((f) => {
          const i = f.draft ? nthDraft++ : -1;
          const lit = f.draft ? ramp(frame, 88 + i * 9, 96 + i * 9) : 0;
          const h = f.draft ? 72 * (1 - gone) : 72;
          return (
            <div key={f.name} style={{ height: h, opacity: f.draft ? 1 - gone : 1, overflow: "hidden", display: "flex", alignItems: "center", justifyContent: "space-between", padding: "0 28px", margin: "0 12px", borderRadius: 10, background: `rgba(255,255,255,${0.09 * lit})` }}>
              <span style={{ display: "flex", alignItems: "center", gap: 16, fontSize: 24, color: f.draft ? C.fg : C.muted }}>
                <span style={{ width: 22, height: 28, borderRadius: 4, border: `1.5px solid ${f.draft ? C.fg : C.dim}` }} />
                {f.name}
              </span>
              <span style={{ fontSize: 18, color: C.dim }}>{f.meta}</span>
            </div>
          );
        })}
        <div style={{ position: "absolute", left: 28, bottom: 24, fontFamily: MONO, fontSize: 18, letterSpacing: "0.12em", textTransform: "uppercase", color: C.muted, opacity: ramp(frame, 226, 240) }}>
          3 items moved to the Trash
        </div>
      </Window>

      {/* The approval card */}
      {card > 0.01 && (
        <div style={{ position: "absolute", left: CARD.x, top: CARD.y, width: CARD.w, height: CARD.h, borderRadius: 20, border: "1px solid rgba(255,255,255,0.28)", background: "#000", boxShadow: "0 30px 80px rgba(0,0,0,0.8)", padding: 32, opacity: card, translate: `0 ${(1 - cardIn) * 40}px`, scale: `${0.96 + 0.04 * cardIn}` }}>
          <div style={{ fontSize: 34, fontWeight: 400, letterSpacing: "-0.02em" }}>Move 3 files to the Trash?</div>
          <div style={{ marginTop: 10, fontFamily: MONO, fontSize: 18, color: C.muted }}>draft-1.key, draft-2.key, draft-3.key</div>
          <div style={{ position: "absolute", left: 32, right: 32, top: 150, display: "flex", gap: 16 }}>
            <div style={{ flex: 1, height: 56, borderRadius: 12, background: C.fg, color: C.bg, display: "grid", placeItems: "center", fontSize: 24, fontWeight: 500, scale: frame >= 184 && frame < 194 ? "0.96" : "1" }}>Move</div>
            <div style={{ flex: 1, height: 56, borderRadius: 12, border: `1px solid ${C.line}`, color: C.muted, display: "grid", placeItems: "center", fontSize: 24 }}>Not now</div>
          </div>
        </div>
      )}

      <Voice text="Yes." start={148} end={178} y={950} />

      <Delay by={160}>
        <SimulatedCursor
          size={40}
          points={[
            { x: 1420, y: 980, hold: 0 },
            { x: MOVE_BTN.x, y: MOVE_BTN.y, hold: 26, click: true },
            { x: 1180, y: 700, hold: 400 },
          ]}
        />
      </Delay>
    </Stage>
  );
}
