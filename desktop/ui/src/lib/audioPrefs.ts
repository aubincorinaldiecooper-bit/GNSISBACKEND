/** The person's audio choices, kept on this computer. */
const MIC_KEY = "gnsis-microphone";
const SPEAK_KEY = "gnsis-speak-replies";

function read(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function write(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* not persisted */
  }
}

export const loadMicrophone = (): string => read(MIC_KEY) ?? "";
export const saveMicrophone = (id: string): void => write(MIC_KEY, id);
export const loadSpeakReplies = (): boolean => read(SPEAK_KEY) !== "0";
export const saveSpeakReplies = (on: boolean): void => write(SPEAK_KEY, on ? "1" : "0");
