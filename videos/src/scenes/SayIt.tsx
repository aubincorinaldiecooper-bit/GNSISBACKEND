import { useCurrentFrame } from "remotion";
import { SimulatedCursor } from "../remocn/simulated-cursor";
import { C, Delay, MONO, ramp, Stage, Status, Typed, Voice, Window } from "../ui/kit";

/**
 * "Make a Groceries note": you say it, GNSIS finds Notes, makes the note,
 * types it, then checks the screen.
 */
export const SAY_IT_FRAMES = 300;

const WIN = { x: 420, y: 230, w: 1080, h: 600 };
const CONTENT_Y = WIN.y + 52;
const NEW_NOTE = { x: WIN.x + WIN.w - 64, y: CONTENT_Y + 34 };

export function SayIt() {
  const frame = useCurrentFrame();
  const made = frame >= 150;
  const listIn = ramp(frame, 150, 162);
  const check = ramp(frame, 228, 244);

  return (
    <Stage>
      <Status
        steps={[
          { at: 0, text: "Listening" },
          { at: 92, text: "Looking · Notes" },
          { at: 140, text: "Acting" },
          { at: 226, text: "Checked on screen ✓" },
        ]}
      />
      <Voice text="In Notes, make a Groceries note | with milk, eggs and bread." start={8} end={90} y={520} />

      <Window title="Notes" appear={96} {...WIN}>
        {/* Sidebar */}
        <div style={{ position: "absolute", left: 0, top: 0, bottom: 0, width: 300, borderRight: `1px solid ${C.line}`, padding: "24px 16px", display: "flex", flexDirection: "column", gap: 8 }}>
          {made && (
            <div style={{ height: 64 * listIn, opacity: listIn, overflow: "hidden", borderRadius: 10, background: C.hi, padding: "12px 16px" }}>
              <div style={{ fontSize: 22, color: C.fg }}>{frame >= 160 ? "Groceries" : "New Note"}</div>
              <div style={{ fontSize: 16, color: C.muted, marginTop: 4 }}>Just now</div>
            </div>
          )}
          {[
            ["Reading list", "Yesterday"],
            ["Ideas", "Monday"],
            ["Trip to Lisbon", "Oct 21"],
          ].map(([t, d]) => (
            <div key={t} style={{ padding: "12px 16px" }}>
              <div style={{ fontSize: 22, color: C.muted }}>{t}</div>
              <div style={{ fontSize: 16, color: C.dim, marginTop: 4 }}>{d}</div>
            </div>
          ))}
        </div>

        {/* New Note button */}
        <div style={{ position: "absolute", left: NEW_NOTE.x - WIN.x - 22, top: 12, width: 44, height: 44, borderRadius: 10, border: `1px solid ${C.line}`, display: "grid", placeItems: "center", background: frame >= 144 && frame < 156 ? "rgba(255,255,255,0.16)" : "transparent" }}>
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke={C.fg} strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
            <path d="M12 20h9" />
            <path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z" />
          </svg>
        </div>

        {/* Editor */}
        {made && (
          <div style={{ position: "absolute", left: 348, top: 96, right: 48, opacity: listIn }}>
            <div style={{ fontSize: 44, fontWeight: 500, letterSpacing: "-0.02em", minHeight: 56 }}>
              <Typed text="Groceries" start={160} perChar={1.7} />
            </div>
            <div style={{ position: "relative", marginTop: 24, fontSize: 30, fontWeight: 300, lineHeight: 1.6, color: C.fg, minHeight: 150 }}>
              <Typed text={"milk\neggs\nbread"} start={185} perChar={1.6} />
              {/* The on-screen check: GNSIS looks at the result before saying it's done. */}
              <div style={{ position: "absolute", left: -20, top: -12, width: 300, height: 170, border: `1px solid ${C.fg}`, borderRadius: 12, opacity: check * 0.9, scale: `${1.04 - 0.04 * check}` }} />
              <div style={{ position: "absolute", left: 300, top: -12, marginLeft: 16, fontFamily: MONO, fontSize: 18, letterSpacing: "0.14em", textTransform: "uppercase", color: C.fg, opacity: check }}>
                ✓ Note is there
              </div>
            </div>
          </div>
        )}
      </Window>

      <Delay by={118}>
        <SimulatedCursor
          size={40}
          points={[
            { x: 1640, y: 960, hold: 4 },
            { x: NEW_NOTE.x - 6, y: NEW_NOTE.y - 6, hold: 34, click: true },
            { x: 1260, y: 560, hold: 400 },
          ]}
        />
      </Delay>
    </Stage>
  );
}
