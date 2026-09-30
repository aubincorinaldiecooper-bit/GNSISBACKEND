/**
 * The app the person was last using, other than GNSIS itself.
 *
 * GNSIS presses keys, types and clicks into "the app in front". Once the
 * person has clicked one of GNSIS's own cards (the voice button, say), that
 * app is GNSIS, and asking macOS which app is in front then only names GNSIS.
 * So each time another app comes to the front, its name is noted here, and an
 * action that types or clicks can put that app back in front first.
 *
 * No Electron import, so it can be tested with stand-ins.
 */
export class PersonApp {
  private last: string | null = null;

  constructor(
    /** GNSIS's own name, as macOS reports it. */
    private readonly self: string,
    /** The name of the app in front right now, or null when it cannot be read. */
    private readonly frontApp: () => Promise<string | null>,
  ) {}

  /** The app to go back to, if one has been seen. */
  get name(): string | null {
    return this.last;
  }

  /** An app came to the front: remember it, unless it is GNSIS. */
  async noteFront(): Promise<void> {
    let name: string | null = null;
    try {
      name = await this.frontApp();
    } catch {
      return;
    }
    if (name && name !== this.self) this.last = name;
  }
}

/**
 * Which app to bring back to the front before GNSIS types or clicks: the
 * person's, but only when GNSIS itself is in front. Null means leave things
 * as they are: someone else's app is already in front, or none is known.
 */
export function appToRestore(front: string | null, self: string, person: string | null): string | null {
  return front === self && person && person !== self ? person : null;
}
