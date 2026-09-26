import type { ReactNode } from "react";

function Icon({ size = 20, sw = 1.8, children }: { size?: number; sw?: number; children: ReactNode }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={sw}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      style={{ flexShrink: 0 }}
    >
      {children}
    </svg>
  );
}
type P = { size?: number; sw?: number };

export const Plus = (p: P) => <Icon {...p}><path d="M12 5v14M5 12h14" /></Icon>;
export const Close = (p: P) => <Icon {...p}><path d="M6 6l12 12M18 6L6 18" /></Icon>;
export const Mic = (p: P) => <Icon {...p}><rect x="9" y="3" width="6" height="11" rx="3" /><path d="M5 11a7 7 0 0 0 14 0M12 18v3" /></Icon>;
export const MicOff = (p: P) => <Icon {...p}><rect x="9" y="3" width="6" height="11" rx="3" /><path d="M5 11a7 7 0 0 0 14 0M12 18v3M3 3l18 18" /></Icon>;
export const Wave = (p: P) => <Icon sw={2.2} {...p}><path d="M4 10v4M8 7v10M12 4v16M16 7v10M20 10v4" /></Icon>;
export const ArrowUp = (p: P) => <Icon sw={2} {...p}><path d="M12 19V5M6 11l6-6 6 6" /></Icon>;
export const ChevronDown = (p: P) => <Icon {...p}><path d="M6 9l6 6 6-6" /></Icon>;
export const ChevronUp = (p: P) => <Icon {...p}><path d="M6 15l6-6 6 6" /></Icon>;
export const ChevronLeft = (p: P) => <Icon {...p}><path d="M15 6l-6 6 6 6" /></Icon>;
export const ChevronRight = (p: P) => <Icon {...p}><path d="M9 6l6 6-6 6" /></Icon>;
export const Check = (p: P) => <Icon sw={2.4} {...p}><path d="M5 12.5l4.5 4.5L19 7.5" /></Icon>;
export const Menu = (p: P) => <Icon {...p}><path d="M4 7h16M4 12h16M4 17h16" /></Icon>;
export const Globe = (p: P) => <Icon {...p}><circle cx="12" cy="12" r="9" /><path d="M3 12h18M12 3a14 14 0 0 1 0 18a14 14 0 0 1 0-18" /></Icon>;
export const Lock = (p: P) => <Icon sw={2} {...p}><rect x="5" y="11" width="14" height="10" rx="2" /><path d="M8 11V8a4 4 0 0 1 8 0v3" /></Icon>;
export const Keyboard = (p: P) => <Icon {...p}><rect x="3" y="6" width="18" height="12" rx="2" /><path d="M7 10h.01M11 10h.01M15 10h.01M17 10h.01M7 14h10" /></Icon>;
export const Mail = (p: P) => <Icon {...p}><rect x="3" y="5" width="18" height="14" rx="2" /><path d="M3 7l9 6 9-6" /></Icon>;
export const Pencil = (p: P) => <Icon {...p}><path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z" /></Icon>;
export const Gear = (p: P) => <Icon {...p}><circle cx="12" cy="12" r="3" /><path d="M12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1" /></Icon>;
export const Dots = (p: P) => <Icon sw={2.6} {...p}><path d="M5 12h.01M12 12h.01M19 12h.01" /></Icon>;
export const Copy = (p: P) => <Icon {...p}><rect x="8" y="8" width="12" height="13" rx="2.5" /><path d="M16 8V5.5A2.5 2.5 0 0 0 13.5 3h-7A2.5 2.5 0 0 0 4 5.5v9A2.5 2.5 0 0 0 6.5 17H8" /></Icon>;
export const Share = (p: P) => <Icon {...p}><path d="M12 15V3M7 8l5-5 5 5M5 12v7a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-7" /></Icon>;
export const PanelIcon = (p: P) => <Icon {...p}><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M15 4v16" /></Icon>;
export const Hand = (p: P) => <Icon {...p}><path d="M9 11V5.5a1.5 1.5 0 0 1 3 0V11M12 10.5V4a1.5 1.5 0 0 1 3 0v6.5M15 10.5V6a1.5 1.5 0 0 1 3 0v8a6 6 0 0 1-6 6h-1a6 6 0 0 1-5-2.7L3.5 13.5a1.5 1.5 0 0 1 2.4-1.8L9 14" /></Icon>;
export const External = (p: P) => <Icon {...p}><path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5" /></Icon>;
export const Screen = (p: P) => <Icon {...p}><rect x="3" y="4" width="18" height="13" rx="2.5" /><path d="M8 21h8M12 17v4" /></Icon>;
export const Camera = (p: P) => <Icon {...p}><rect x="3" y="6" width="13" height="12" rx="3" /><path d="M16 10.5l5-3v9l-5-3" /></Icon>;
export const Eye = (p: P) => <Icon {...p}><path d="M2.5 12s3.5-6.5 9.5-6.5S21.5 12 21.5 12s-3.5 6.5-9.5 6.5S2.5 12 2.5 12z" /><circle cx="12" cy="12" r="3" /></Icon>;
export const EyeOff = (p: P) => <Icon {...p}><path d="M3 3l18 18M10.6 5.9A10.5 10.5 0 0 1 12 5.5c6 0 9.5 6.5 9.5 6.5a17 17 0 0 1-3.2 3.9M6.6 6.6C3.9 8.6 2.5 12 2.5 12s3.5 6.5 9.5 6.5c1.6 0 3-.4 4.2-1M9.9 9.9a3 3 0 0 0 4.2 4.2" /></Icon>;
