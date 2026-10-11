import type { CSSProperties } from "react";
import { Blobatar } from "@blobatar/react";
import { traits } from "blobatar";
import { happy, thinking } from "blobatar/expression";
import type { Presence } from "../store/store";

/** blobatar's character on the app icon, and GNSIS's face before a person has their own. */
export const GNSIS_ICON_NAME = "gnsis-h";

const icon = traits(GNSIS_ICON_NAME);

/**
 * GNSIS's look: the mint cloud (the owner's pick, 30 September). Every face
 * has the icon character's colour and cloud silhouette, read from blobatar
 * rather than copied (the hue is drawn exactly as blobatar draws it, from 0 to
 * 360; the tone is the icon's 0.8). Everything else (eyes, puffs, size, tilt)
 * comes from the device's public ID, so every person's GNSIS is their own mint
 * cloud.
 */
export const GNSIS_FACE = { hue: icon.num("hue", 0, 360), tone: 0.8, traits: { shape: icon("shape") } } as const;

/** blobatar leaves margin inside its 100×100 box; draw a little larger so faces fill their slot. */
const FILL = 1.3;

interface FaceProps {
  name: string;
  size: number;
  gnsis?: boolean;
  working?: boolean;
  joyful?: boolean;
  /** extra transform for speaking / listening reactions */
  style?: CSSProperties;
  pop?: boolean;
}

export function Face({ name, size, gnsis, working, joyful, style, pop }: FaceProps) {
  const expression = joyful ? happy : working ? thinking : undefined;
  return (
    <span className={"face" + (pop ? " face-pop" : "")} style={{ width: size, height: size, ...style }} aria-hidden="true">
      <Blobatar
        name={name}
        size={Math.round(size * FILL)}
        animate="always"
        expression={expression}
        style={{ margin: -Math.round((size * (FILL - 1)) / 2) }}
        {...(gnsis ? GNSIS_FACE : {})}
      />
    </span>
  );
}

/** Face + presence: green dot working, orange "!" needs you, gray done, black badge new result. */
export function AgentFace({
  name,
  size,
  p,
  markSize = "md",
  pop,
}: {
  name: string;
  size: number;
  p?: Presence;
  markSize?: "sm" | "md" | "lg";
  pop?: boolean;
}) {
  return (
    <span className={"agent-face mark-" + markSize}>
      <Face name={name} size={size} working={p?.working} pop={pop} />
      {p && !p.needs && <span className={"mark-dot" + (p.working ? " is-working" : "")} />}
      {p && p.needs && <span className="mark-needs">!</span>}
      {p && p.unread && <span className="mark-badge">{markSize === "sm" ? "" : "1"}</span>}
    </span>
  );
}

export function ThinkingDots() {
  return (
    <span className="thinking-dots" aria-hidden="true">
      <span />
      <span />
      <span />
    </span>
  );
}
