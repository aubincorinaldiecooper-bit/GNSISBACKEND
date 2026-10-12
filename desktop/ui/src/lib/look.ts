/** The glass look the person picked in Settings, kept on this computer. Clear unless they chose dark. */
export type Look = "clear" | "dark";

const KEY = "gnsis-look";

export function loadLook(): Look {
  try {
    return localStorage.getItem(KEY) === "dark" ? "dark" : "clear";
  } catch {
    return "clear";
  }
}

export function saveLook(look: Look): void {
  try {
    localStorage.setItem(KEY, look);
  } catch {
    /* not persisted */
  }
}

interface ClassTarget {
  classList: { toggle(name: string, on: boolean): unknown };
}

export function applyLook(look: Look, target: ClassTarget = document.body): void {
  target.classList.toggle("gnsis-dark", look === "dark");
}
