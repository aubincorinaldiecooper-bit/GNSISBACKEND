/**
 * Deliberately narrow no-model routing for commands whose meaning is fully
 * contained in the person's words. Anything ambiguous or multi-step goes to
 * the foreground model instead of being guessed here.
 */

export interface LocalCommand {
  tool: "open";
  args: { target: string };
}

const OPEN = /^(?:please\s+)?(?:open|launch|start)\s+(.+?)(?:\s+please)?[.!]?$/i;
const UNSAFE_JOIN = /\b(?:and|then|after|before|while|but)\b/i;

export function localCommand(input: string): LocalCommand | null {
  const text = input.trim();
  if (!text || text.length > 240 || UNSAFE_JOIN.test(text)) return null;
  const match = OPEN.exec(text);
  const target = match?.[1]?.trim();
  if (!target || target.length > 180) return null;
  return { tool: "open", args: { target } };
}
