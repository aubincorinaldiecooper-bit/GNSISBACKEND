/**
 * `files`: see and change files and folders on the person's machine.
 *
 * Names are resolved the way a person means them: "selected" is what they
 * selected in Finder, "Downloads" is their Downloads folder, and a bare name
 * is looked for in the Finder window they have open and the usual folders.
 * More than one match is never guessed at — the candidates go back to the
 * model to ask about.
 *
 * What this will not do, whoever asks: delete, move to the Trash, overwrite,
 * or touch system and credential folders. Those are refused before anything
 * is prepared, so no confirmation can talk it into them.
 */
import { constants as fsConstants, promises as fs, type Dirent } from "node:fs";
import path from "node:path";
import {
  ActionProblem,
  required,
  str,
  type ActionTool,
  type PreparedAction,
  type ScopeTarget,
} from "./actions.js";

/** What Finder can tell us on a Mac; absent elsewhere. */
export interface FinderBridge {
  /** Absolute paths of the items selected in Finder, front window first. */
  selection(): Promise<string[]>;
  /** The folder the front Finder window shows, if there is one. */
  frontFolder(): Promise<string | null>;
  /** Spotlight name search under a folder; null when Spotlight is unavailable. */
  search?(folder: string, query: string): Promise<string[] | null>;
}

export interface FilesOptions {
  home: string;
  finder?: FinderBridge;
  /** For tests: a fixed clock for "modified" labels. */
  now?: () => number;
}

const SELECTION_WORDS = new Set(["selected", "selection", "this", "that", "it", "this file", "that file", "this folder", "that folder"]);
const KNOWN_FOLDERS: Record<string, string> = {
  home: "",
  "home folder": "",
  desktop: "Desktop",
  documents: "Documents",
  downloads: "Downloads",
  pictures: "Pictures",
  music: "Music",
  movies: "Movies",
  applications: "Applications",
};
const MAX_LISTED = 20;
const MAX_FOUND = 10;
const WALK_LIMIT = 20_000;

export class FilesTool implements ActionTool {
  readonly name = "files";
  readonly platforms = ["darwin", "linux"] as const;

  constructor(private readonly opts: FilesOptions) {}

  async prepare(args: Record<string, unknown>): Promise<PreparedAction> {
    const action = str(args, "action");
    switch (action) {
      case "list":
        return this.prepareList(args);
      case "find":
        return this.prepareFind(args);
      case "move":
        return this.prepareMove(args);
      case "rename":
        return this.prepareRename(args);
      case "new_folder":
        return this.prepareNewFolder(args);
      default:
        throw new ActionProblem("unsupported", `files cannot ${String(action)}.`);
    }
  }

  // --- reading ----------------------------------------------------------

  private async prepareList(args: Record<string, unknown>): Promise<PreparedAction> {
    const folder = str(args, "path")
      ? (await this.resolve(str(args, "path")!, "folder")).path
      : (await this.defaultFolder());
    await this.ensureAllowed(folder, "look in");
    return {
      tool: this.name,
      action: "list",
      effect: "read",
      summary: `List what is in ${this.show(folder)}`,
      scope: [],
      run: async () => {
        const entries = await fs.readdir(folder, { withFileTypes: true });
        const visible = entries.filter((e) => !e.name.startsWith("."));
        const items = await this.describeAll(folder, visible);
        items.sort((a, b) => b.mtime - a.mtime);
        return {
          verified: "disk",
          message: `${this.show(folder)} has ${visible.length} item${visible.length === 1 ? "" : "s"}.`,
          detail: {
            folder: this.show(folder),
            count: visible.length,
            newest_first: items.slice(0, MAX_LISTED).map((i) => i.label),
          },
        };
      },
    };
  }

  private async prepareFind(args: Record<string, unknown>): Promise<PreparedAction> {
    const query = required(args, "query", "what to look for");
    const folder = str(args, "path") ? (await this.resolve(str(args, "path")!, "folder")).path : this.opts.home;
    await this.ensureAllowed(folder, "look in");
    return {
      tool: this.name,
      action: "find",
      effect: "read",
      summary: `Look for “${query}” in ${this.show(folder)}`,
      scope: [],
      run: async () => {
        const matches = await this.findUnder(folder, query);
        const described = await Promise.all(
          matches.map(async (p) => ({ p, mtime: await mtimeOf(p) })),
        );
        described.sort((a, b) => b.mtime - a.mtime);
        const found = described.slice(0, MAX_FOUND).map((d) => `${this.show(d.p)} (${this.age(d.mtime)})`);
        return {
          verified: "disk",
          message: found.length
            ? `Found ${described.length} match${described.length === 1 ? "" : "es"} for “${query}”, newest first.`
            : `Nothing in ${this.show(folder)} matches “${query}”.`,
          detail: { query, folder: this.show(folder), found },
        };
      },
    };
  }

  // --- changing -----------------------------------------------------------

  private async prepareMove(args: Record<string, unknown>): Promise<PreparedAction> {
    const source = await this.resolve(required(args, "path", "what to move"), "any");
    const destination = await this.resolve(required(args, "to", "where to move it"), "folder");
    await this.ensureChangeable(source.path, "move");
    await this.ensureAllowed(destination.path, "move things into");
    if (isTrash(destination.path, this.opts.home)) {
      throw new ActionProblem("refused", "GNSIS does not delete files or move them to the Trash.");
    }
    if (destination.path === path.dirname(source.path)) {
      throw new ActionProblem("failed", `${path.basename(source.path)} is already in ${this.show(destination.path)}.`);
    }
    if (isInside(destination.path, source.path)) {
      throw new ActionProblem("refused", "A folder cannot be moved into itself.");
    }
    const target = path.join(destination.path, path.basename(source.path));
    if (await exists(target)) {
      throw new ActionProblem(
        "refused",
        `${this.show(destination.path)} already has something called ${path.basename(source.path)}. GNSIS does not replace files.`,
      );
    }
    return {
      tool: this.name,
      action: "move",
      effect: "change",
      summary: `Move “${path.basename(source.path)}” into “${path.basename(destination.path) || this.show(destination.path)}”`,
      scope: [source.scope, destination.scope],
      run: async () => {
        await moveNoClobber(source.path, target);
        const arrived = await exists(target);
        const left = !(await exists(source.path));
        if (!arrived || !left) {
          throw new ActionProblem("failed", "The move did not finish: the file is not where it should be.", {
            at_destination: arrived,
            gone_from_source: left,
          });
        }
        return {
          verified: "disk",
          message: `Moved ${path.basename(source.path)} into ${this.show(destination.path)}.`,
          detail: { from: this.show(source.path), to: this.show(target) },
        };
      },
    };
  }

  private async prepareRename(args: Record<string, unknown>): Promise<PreparedAction> {
    const source = await this.resolve(required(args, "path", "what to rename"), "any");
    const requested = required(args, "name", "the new name");
    await this.ensureChangeable(source.path, "rename");
    const stat = await fs.stat(source.path);
    const newName = keepExtension(path.basename(source.path), cleanName(requested), stat.isDirectory());
    const target = path.join(path.dirname(source.path), newName);
    if (target === source.path) {
      throw new ActionProblem("failed", `It is already called ${newName}.`);
    }
    if (await exists(target) && target.toLowerCase() !== source.path.toLowerCase()) {
      throw new ActionProblem("refused", `There is already something called ${newName} there. GNSIS does not replace files.`);
    }
    return {
      tool: this.name,
      action: "rename",
      effect: "change",
      summary: `Rename “${path.basename(source.path)}” to “${newName}”`,
      scope: [source.scope, { value: requested, source: "named" }],
      run: async () => {
        // The name may have been taken while the person read the question:
        // the no-replace check happens again, atomically, as it runs.
        await moveNoClobber(source.path, target);
        if (!(await exists(target))) {
          throw new ActionProblem("failed", "The rename did not take.");
        }
        return {
          verified: "disk",
          message: `Renamed ${path.basename(source.path)} to ${newName}.`,
          detail: { now: this.show(target) },
        };
      },
    };
  }

  private async prepareNewFolder(args: Record<string, unknown>): Promise<PreparedAction> {
    const name = cleanName(required(args, "name", "the folder's name"));
    const parent = str(args, "path") ? (await this.resolve(str(args, "path")!, "folder")).path : await this.defaultFolder();
    await this.ensureAllowed(parent, "make folders in");
    const target = path.join(parent, name);
    if (await exists(target)) {
      throw new ActionProblem("failed", `${this.show(parent)} already has something called ${name}.`);
    }
    return {
      tool: this.name,
      action: "new_folder",
      effect: "change",
      summary: `Make a folder “${name}” in ${this.show(parent)}`,
      scope: [{ value: name, source: "named" }],
      run: async () => {
        // mkdir without `recursive` fails if anything is already there.
        await fs.mkdir(target).catch((err: NodeJS.ErrnoException) => {
          if (err.code === "EEXIST") throw new ActionProblem("failed", `${this.show(parent)} already has something called ${name}.`);
          throw err;
        });
        const made = await fs.stat(target).then((s) => s.isDirectory(), () => false);
        if (!made) throw new ActionProblem("failed", "The folder was not made.");
        return { verified: "disk", message: `Made ${this.show(target)}.`, detail: { folder: this.show(target) } };
      },
    };
  }

  // --- resolving names ------------------------------------------------------

  /** Turn what the model said into one real path, or explain why not. */
  async resolve(
    said: string,
    kind: "file" | "folder" | "any",
  ): Promise<{ path: string; scope: ScopeTarget }> {
    const text = said.trim();
    const lower = text.toLowerCase();
    if (SELECTION_WORDS.has(lower)) {
      const selected = (await this.opts.finder?.selection()) ?? [];
      if (selected.length === 0) {
        throw new ActionProblem("not_found", "Nothing is selected in Finder. Select it first, or say its name.");
      }
      if (selected.length > 1) {
        throw new ActionProblem("ambiguous", `${selected.length} items are selected in Finder; say which one.`, {
          candidates: selected.slice(0, 5).map((p) => this.show(p)),
        });
      }
      await this.checkKind(selected[0], kind);
      return { path: selected[0], scope: { value: path.basename(selected[0]), source: "selection" } };
    }
    const scope: ScopeTarget = { value: text, source: "named" };
    const known = KNOWN_FOLDERS[lower];
    if (known !== undefined) {
      return { path: path.join(this.opts.home, known), scope };
    }
    if (text.startsWith("~") || path.isAbsolute(text) || text.includes("/")) {
      const full = path.resolve(
        text.startsWith("~") ? path.join(this.opts.home, text.slice(1)) : path.isAbsolute(text) ? text : path.join(this.opts.home, text),
      );
      if (!(await exists(full))) {
        throw new ActionProblem("not_found", `There is nothing at ${this.show(full)}.`);
      }
      await this.checkKind(full, kind);
      return { path: full, scope: { value: path.basename(full), source: "named" } };
    }
    const matches = await this.lookFor(text, kind);
    if (matches.length === 0) {
      throw new ActionProblem(
        "not_found",
        `No ${kind === "folder" ? "folder" : kind === "file" ? "file" : "file or folder"} called ${text} in ${this.opts.finder ? "the open Finder window, " : ""}Desktop, Documents, Downloads or the home folder.`,
      );
    }
    if (matches.length > 1) {
      throw new ActionProblem("ambiguous", `More than one ${text}; say which.`, {
        candidates: matches.slice(0, 5).map((p) => this.show(p)),
      });
    }
    return { path: matches[0], scope };
  }

  private async lookFor(name: string, kind: "file" | "folder" | "any"): Promise<string[]> {
    // The open Finder window is looked in first. If Finder cannot be asked
    // (the person has not allowed it), the usual folders are still searched,
    // and if the name is not in them the refusal is what is reported — never
    // a "not found" that claims the window was looked in.
    let front: string | null = null;
    let finderProblem: ActionProblem | null = null;
    try {
      front = (await this.opts.finder?.frontFolder()) ?? null;
    } catch (err) {
      finderProblem = err instanceof ActionProblem ? err : new ActionProblem("failed", `Finder did not answer: ${String(err)}`);
    }
    const found = await this.lookIn(name, kind, front);
    if (found.length === 0 && finderProblem) throw finderProblem;
    return found;
  }

  private async lookIn(name: string, kind: "file" | "folder" | "any", front: string | null): Promise<string[]> {
    const roots = unique(
      [front, "Desktop", "Documents", "Downloads", ""].flatMap((r) =>
        r == null ? [] : [r.startsWith("/") ? r : path.join(this.opts.home, r)],
      ),
    );
    const wanted = name.toLowerCase();
    // Nearby first: the open window and the usual folders, one level deep.
    for (const exact of [true, false]) {
      const hits: string[] = [];
      for (const root of roots) {
        const entries = await fs.readdir(root, { withFileTypes: true }).catch(() => [] as Dirent[]);
        for (const entry of entries) {
          if (entry.name.startsWith(".")) continue;
          const entryName = entry.name.toLowerCase();
          const same = exact ? entryName === wanted : stripExtension(entryName) === wanted;
          if (same && kindMatches(entry, kind)) hits.push(path.join(root, entry.name));
        }
      }
      if (hits.length) return unique(hits);
    }
    // Then one level further down the usual folders.
    const deeper: string[] = [];
    for (const root of roots.filter((r) => r !== this.opts.home)) {
      const children = await fs.readdir(root, { withFileTypes: true }).catch(() => [] as Dirent[]);
      for (const child of children) {
        if (!child.isDirectory() || child.name.startsWith(".")) continue;
        const inner = await fs.readdir(path.join(root, child.name), { withFileTypes: true }).catch(() => [] as Dirent[]);
        for (const entry of inner) {
          if (entry.name.toLowerCase() === wanted && kindMatches(entry, kind)) {
            deeper.push(path.join(root, child.name, entry.name));
          }
        }
      }
    }
    return unique(deeper);
  }

  private async findUnder(folder: string, query: string): Promise<string[]> {
    const wanted = query.toLowerCase().replace(/^\*?\./, "");
    const isKind = /^[a-z0-9]{1,6}$/.test(wanted);
    const matches = (name: string) => {
      const lower = name.toLowerCase();
      return lower.includes(wanted) || (isKind && lower.endsWith(`.${wanted}`));
    };
    const spotlight = await this.opts.finder?.search?.(folder, query).catch(() => null);
    if (spotlight) {
      return spotlight.filter((p) => !isHidden(p, folder) && matches(path.basename(p)));
    }
    const found: string[] = [];
    let visited = 0;
    const walk = async (dir: string, depth: number) => {
      if (depth > 4 || visited > WALK_LIMIT) return;
      const entries = await fs.readdir(dir, { withFileTypes: true }).catch(() => [] as Dirent[]);
      for (const entry of entries) {
        visited += 1;
        if (entry.name.startsWith(".") || entry.name === "node_modules" || entry.name === "Library") continue;
        const full = path.join(dir, entry.name);
        if (matches(entry.name)) found.push(full);
        if (entry.isDirectory() && !entry.name.endsWith(".app")) await walk(full, depth + 1);
      }
    };
    await walk(folder, 0);
    return found;
  }

  /** The folder the person has open in Finder, else Desktop. A Finder refusal is reported, not papered over. */
  private async defaultFolder(): Promise<string> {
    const front = (await this.opts.finder?.frontFolder()) ?? null;
    return front ?? path.join(this.opts.home, "Desktop");
  }

  private async checkKind(full: string, kind: "file" | "folder" | "any"): Promise<void> {
    if (kind === "any") return;
    const isDir = await fs.stat(full).then((s) => s.isDirectory(), () => false);
    if (kind === "folder" && !isDir) throw new ActionProblem("not_found", `${this.show(full)} is a file, not a folder.`);
    if (kind === "file" && isDir) throw new ActionProblem("not_found", `${this.show(full)} is a folder, not a file.`);
  }

  // --- what is off limits ---------------------------------------------------

  /** Refuse to change a protected path, or a link, wherever it really leads. */
  private async ensureChangeable(full: string, verb: string): Promise<void> {
    await this.ensureAllowed(full, verb);
    const stat = await fs.lstat(full);
    if (stat.isSymbolicLink()) {
      throw new ActionProblem("refused", `${path.basename(full)} is a link to somewhere else. GNSIS does not ${verb} links.`);
    }
    const home = this.opts.home;
    const protectedRoots = [home, ...Object.values(KNOWN_FOLDERS).filter(Boolean).map((f) => path.join(home, f))];
    const real = await this.realOf(full);
    const realRoots = await Promise.all(protectedRoots.map((root) => this.realOf(root)));
    if (protectedRoots.includes(full) || realRoots.includes(real)) {
      throw new ActionProblem("refused", `GNSIS does not ${verb} ${this.show(full)} itself.`);
    }
  }

  /**
   * Refuse a path outside the person's own files, or in hidden and system
   * folders — both as written and where it really leads, so a folder that is
   * a link into ~/.ssh or out of the home folder is refused like the place it
   * points at. A path that does not exist yet is judged by its folder.
   */
  async ensureAllowed(full: string, verb: string): Promise<void> {
    this.refuseSensitive(full, verb, this.opts.home);
    const real = await this.realOf(full);
    this.refuseSensitive(real, verb, await this.realOf(this.opts.home), true);
  }

  /** Where a path really is: links resolved; for a new path, its folder's. */
  private async realOf(full: string): Promise<string> {
    const real = await fs.realpath(full).catch(() => null);
    if (real) return real;
    const parent = await fs.realpath(path.dirname(full)).catch(() => path.dirname(full));
    return path.join(parent, path.basename(full));
  }

  private refuseSensitive(full: string, verb: string, home: string, resolved = false): void {
    const insideHome = full === home || full.startsWith(home + path.sep);
    const relative = insideHome ? path.relative(home, full) : "";
    const parts = relative.split(path.sep).filter(Boolean).map((p) => p.toLowerCase());
    if (!insideHome && !full.startsWith("/Volumes/")) {
      throw new ActionProblem(
        "refused",
        resolved
          ? `That leads outside your own files. GNSIS does not ${verb} it.`
          : `GNSIS only works with your own files, not ${full}.`,
      );
    }
    // Library is system territory, except where macOS keeps the person's own
    // cloud files: iCloud Drive, and Dropbox / Google Drive / OneDrive, which
    // are often reached through a link from the home folder.
    const cloud = parts[0] === "library" && (parts[1] === "mobile documents" || parts[1] === "cloudstorage");
    if ((parts[0] === "library" && !cloud) || parts.some((p) => p.startsWith("."))) {
      throw new ActionProblem(
        "refused",
        resolved
          ? `That leads into a hidden or system folder. GNSIS does not ${verb} it.`
          : `GNSIS does not ${verb} hidden or system folders.`,
      );
    }
  }

  // --- presentation ---------------------------------------------------------

  show(full: string): string {
    const home = this.opts.home;
    if (full === home) return "your home folder";
    return full.startsWith(home + path.sep) ? `~/${path.relative(home, full)}` : full;
  }

  private age(mtime: number): string {
    const minutes = Math.max(0, Math.round(((this.opts.now?.() ?? Date.now()) - mtime) / 60_000));
    if (minutes < 1) return "just now";
    if (minutes < 60) return `${minutes} min ago`;
    const hours = Math.round(minutes / 60);
    if (hours < 48) return `${hours} h ago`;
    return `${Math.round(hours / 24)} days ago`;
  }

  private async describeAll(folder: string, entries: Dirent[]) {
    return Promise.all(
      entries.map(async (entry) => {
        const mtime = await mtimeOf(path.join(folder, entry.name));
        return { mtime, label: `${entry.name}${entry.isDirectory() ? "/" : ""} (${this.age(mtime)})` };
      }),
    );
  }
}

// --- helpers ---------------------------------------------------------------

async function exists(full: string): Promise<boolean> {
  return fs.lstat(full).then(() => true, () => false);
}

async function mtimeOf(full: string): Promise<number> {
  return fs.stat(full).then((s) => s.mtimeMs, () => 0);
}

function kindMatches(entry: Dirent, kind: "file" | "folder" | "any"): boolean {
  if (kind === "any") return true;
  const isDir = entry.isDirectory() && !entry.name.endsWith(".app");
  return kind === "folder" ? isDir : !isDir;
}

function stripExtension(name: string): string {
  const ext = path.extname(name);
  return ext ? name.slice(0, -ext.length) : name;
}

function keepExtension(oldName: string, newName: string, isDirectory: boolean): string {
  if (isDirectory || path.extname(newName)) return newName;
  const ext = path.extname(oldName);
  return ext ? `${newName}${ext}` : newName;
}

function cleanName(name: string): string {
  const trimmed = name.trim();
  if (!trimmed || trimmed === "." || trimmed === ".." || /[/:\u0000]/.test(trimmed) || trimmed.startsWith(".")) {
    throw new ActionProblem("refused", `“${name}” cannot be used as a name.`);
  }
  if (Buffer.byteLength(trimmed, "utf8") > 255) throw new ActionProblem("refused", "That name is too long.");
  return trimmed;
}

function isInside(child: string, parent: string): boolean {
  return child === parent || child.startsWith(parent + path.sep);
}

function isTrash(full: string, home: string): boolean {
  return isInside(full, path.join(home, ".Trash")) || /\/\.Trashes(\/|$)/.test(full);
}

function isHidden(full: string, under: string): boolean {
  return path.relative(under, full).split(path.sep).some((p) => p.startsWith("."));
}

function unique<T>(items: T[]): T[] {
  return [...new Set(items)];
}

/**
 * Move without ever replacing something already at the destination — checked
 * atomically at the moment of the move, not only when it was prepared, since
 * a file can appear there while the person reads the confirmation.
 *
 * The destination name is claimed with an operation that itself fails if the
 * name is taken: a hard link for a file (then the original name is removed),
 * a fresh empty folder for a folder (which the rename then replaces — the only
 * thing a folder rename may replace is an empty folder). If the name turns out
 * to be the very same file (a change of case on a case-insensitive disk), it
 * is renamed in place.
 */
async function moveNoClobber(from: string, to: string): Promise<void> {
  const taken = () => new ActionProblem("refused", "Something with that name is already there. GNSIS does not replace files.");
  const stat = await fs.lstat(from);
  if (stat.isSymbolicLink()) throw new ActionProblem("refused", `${path.basename(from)} is a link. GNSIS does not move links.`);
  if (stat.isDirectory()) {
    try {
      await fs.mkdir(to);
    } catch (err) {
      if ((err as NodeJS.ErrnoException).code !== "EEXIST") throw err;
      if (await sameEntry(from, to)) return fs.rename(from, to);
      throw taken();
    }
    try {
      await fs.rename(from, to);
    } catch (err) {
      if ((err as NodeJS.ErrnoException).code !== "EXDEV") {
        await fs.rmdir(to).catch(() => {});
        throw err;
      }
      // Another disk: copy into the folder just claimed, then remove the original.
      await fs.cp(from, to, { recursive: true, errorOnExist: true, force: false, preserveTimestamps: true });
      await fs.rm(from, { recursive: true });
    }
    return;
  }
  try {
    await fs.link(from, to);
  } catch (err) {
    const code = (err as NodeJS.ErrnoException).code ?? "";
    if (code === "EEXIST") {
      if (await sameEntry(from, to)) return fs.rename(from, to);
      throw taken();
    }
    if (!["EXDEV", "EPERM", "ENOTSUP", "EOPNOTSUPP", "EMLINK", "ENOSYS"].includes(code)) throw err;
    // Another disk, or one without hard links: an exclusive copy.
    try {
      await fs.copyFile(from, to, fsConstants.COPYFILE_EXCL);
    } catch (copyErr) {
      if ((copyErr as NodeJS.ErrnoException).code === "EEXIST") throw taken();
      throw copyErr;
    }
  }
  await fs.unlink(from);
}

/** Two names for the same file or folder (a change of case on a case-insensitive disk). */
async function sameEntry(a: string, b: string): Promise<boolean> {
  const [x, y] = await Promise.all([fs.lstat(a).catch(() => null), fs.lstat(b).catch(() => null)]);
  return x != null && y != null && x.ino === y.ino && x.dev === y.dev;
}
