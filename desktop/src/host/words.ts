/**
 * Matching names against what the person said: loose about case, accents,
 * punctuation and spacing, strict about the words themselves.
 */

/** Lower case, accents dropped. */
export function fold(text: string): string {
  return text.toLowerCase().normalize("NFKD").replace(/[̀-ͯ]/g, "");
}

/** Letters and digits only. */
export function normalize(text: string): string {
  return fold(text).replace(/[^a-z0-9]+/g, "");
}

/** The person's words, in order. */
export function wordsOf(text: string): string[] {
  return fold(text).split(/[^a-z0-9]+/).filter(Boolean);
}

/**
 * Is this name in what the person said? Loose about case, punctuation, a file
 * extension and spacing ("Q3 report" matches "q3-report.pdf", "git hub"
 * matches "GitHub"), strict about the words themselves: the name has to be
 * one of their words, or several of them in a row, never a piece of one.
 * "Open YouTube and search Andrew Tate" names "YouTube", not "ubeand" or
 * "you".
 */
export function saidIn(name: string, words: string): boolean {
  const whole = normalize(name);
  if (!whole) return false;
  const stem = normalize(name.replace(/\.[a-z0-9]{1,6}$/i, ""));
  const wanted = new Set([whole, stem].filter(Boolean));
  const longest = Math.max(...[...wanted].map((w) => w.length));
  const said = wordsOf(words);
  for (let i = 0; i < said.length; i += 1) {
    let run = "";
    for (let j = i; j < said.length && run.length < longest; j += 1) {
      run += said[j];
      if (wanted.has(run)) return true;
    }
  }
  return false;
}
