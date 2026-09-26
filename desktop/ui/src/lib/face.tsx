import type { CSSProperties } from "react";
import { Blobatar } from "@blobatar/react";
import { happy, thinking } from "blobatar/expression";
import type { Presence } from "../store/store";

/**
 * GNSIS's look. The face is generated from the device's public ID, so every user's
 * GNSIS is their own; the sun silhouette and colour are pinned to match the brand.
 * blobatar's hue is OKLCH, so hue 118 + tone 0.8 gives the lime of the setup mockups
 * (hue 78 in blobatar renders amber).
 */
export const GNSIS_FACE = { hue: 118, tone: 0.8, traits: { shape: 0.95 } } as const;

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
