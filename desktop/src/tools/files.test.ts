import assert from "node:assert/strict";
import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import { ActionProblem } from "./actions.js";
import { FilesTool, type FinderBridge } from "./files.js";

async function home(): Promise<string> {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "gnsis-home-"));
  for (const dir of ["Desktop", "Documents", "Downloads", "Documents/Projects", ".Trash", "Library"]) {
    await fs.mkdir(path.join(root, dir), { recursive: true });
  }
  await fs.writeFile(path.join(root, "Downloads", "report.pdf"), "pdf");
  await fs.writeFile(path.join(root, "Downloads", "notes.txt"), "txt");
  return root;
}

async function problem(promise: Promise<unknown>): Promise<ActionProblem> {
  try {
    await promise;
  } catch (err) {
    assert.ok(err instanceof ActionProblem, String(err));
    return err;
  }
  assert.fail("expected an ActionProblem");
}

test("move: a named file goes into a named folder, and the disk is checked", async () => {
  const root = await home();
  const files = new FilesTool({ home: root });
  const move = await files.prepare({ action: "move", path: "report.pdf", to: "Projects" });
  assert.equal(move.effect, "change");
  assert.equal(move.summary, "Move “report.pdf” into “Projects”");
  // Preparing changes nothing.
  await fs.access(path.join(root, "Downloads", "report.pdf"));
  const done = await move.run();
  assert.equal(done.verified, "disk");
  await fs.access(path.join(root, "Documents", "Projects", "report.pdf"));
  await assert.rejects(fs.access(path.join(root, "Downloads", "report.pdf")));
});

test("move: what is selected in Finder is the person's own choice", async () => {
  const root = await home();
  const finder: FinderBridge = {
    selection: async () => [path.join(root, "Downloads", "notes.txt")],
    frontFolder: async () => path.join(root, "Downloads"),
  };
  const move = await new FilesTool({ home: root, finder }).prepare({ action: "move", path: "selected", to: "Desktop" });
  assert.deepEqual(move.scope, [
    { value: "notes.txt", source: "selection" },
    { value: "Desktop", source: "named" },
  ]);
  await move.run();
  await fs.access(path.join(root, "Desktop", "notes.txt"));
});

test("move never replaces a file, never trashes, never touches hidden or system folders", async () => {
  const root = await home();
  await fs.writeFile(path.join(root, "Documents", "Projects", "report.pdf"), "old");
  const files = new FilesTool({ home: root });
  assert.equal((await problem(files.prepare({ action: "move", path: "report.pdf", to: "Projects" }))).status, "refused");
  assert.equal((await problem(files.prepare({ action: "move", path: "notes.txt", to: "~/.Trash" }))).status, "refused");
  assert.equal((await problem(files.prepare({ action: "move", path: "notes.txt", to: "~/Library" }))).status, "refused");
  assert.equal((await problem(files.prepare({ action: "move", path: "Downloads", to: "Desktop" }))).status, "refused");
  assert.equal((await problem(files.prepare({ action: "list", path: "/etc" }))).status, "refused");
  // And nothing moved.
  assert.equal(await fs.readFile(path.join(root, "Documents", "Projects", "report.pdf"), "utf8"), "old");
  await fs.access(path.join(root, "Downloads", "notes.txt"));
});

test("two things with the same name are never guessed between", async () => {
  const root = await home();
  await fs.mkdir(path.join(root, "Desktop", "Projects"));
  const err = await problem(new FilesTool({ home: root }).prepare({ action: "move", path: "report.pdf", to: "Projects" }));
  assert.equal(err.status, "ambiguous");
  assert.deepEqual(err.detail.candidates, ["~/Desktop/Projects", "~/Documents/Projects"]);
});

test("a name that is not there says where it looked", async () => {
  const root = await home();
  const err = await problem(new FilesTool({ home: root }).prepare({ action: "move", path: "report.pdf", to: "Taxes" }));
  assert.equal(err.status, "not_found");
  assert.match(err.message, /No folder called Taxes/);
});

test("rename keeps the extension the person did not mention", async () => {
  const root = await home();
  const rename = await new FilesTool({ home: root }).prepare({ action: "rename", path: "report.pdf", name: "Q3 report" });
  assert.equal(rename.summary, "Rename “report.pdf” to “Q3 report.pdf”");
  await rename.run();
  await fs.access(path.join(root, "Downloads", "Q3 report.pdf"));
});

test("names that would escape the folder or hide the file are refused", async () => {
  const root = await home();
  const files = new FilesTool({ home: root });
  for (const name of ["../evil", "a/b", ".hidden", ".."]) {
    assert.equal((await problem(files.prepare({ action: "rename", path: "notes.txt", name }))).status, "refused", name);
  }
});

test("new_folder makes it where the person is looking", async () => {
  const root = await home();
  const finder: FinderBridge = { selection: async () => [], frontFolder: async () => path.join(root, "Documents") };
  const make = await new FilesTool({ home: root, finder }).prepare({ action: "new_folder", name: "Invoices" });
  assert.equal(make.summary, "Make a folder “Invoices” in ~/Documents");
  await make.run();
  assert.ok((await fs.stat(path.join(root, "Documents", "Invoices"))).isDirectory());
});

test("list and find read only, newest first", async () => {
  const root = await home();
  const files = new FilesTool({ home: root });
  const list = await files.prepare({ action: "list", path: "Downloads" });
  assert.equal(list.effect, "read");
  const listed = await list.run();
  assert.equal(listed.detail?.count, 2);
  const find = await files.prepare({ action: "find", path: "Downloads", query: "pdf" });
  const found = await find.run();
  assert.equal((found.detail?.found as string[]).length, 1);
  assert.match((found.detail?.found as string[])[0], /^~\/Downloads\/report\.pdf/);
});

test("a folder that is really a link into a hidden folder or out of home is refused like where it leads", async () => {
  const root = await home();
  await fs.mkdir(path.join(root, ".ssh"));
  await fs.writeFile(path.join(root, ".ssh", "id_ed25519"), "secret");
  await fs.symlink(path.join(root, ".ssh"), path.join(root, "Documents", "safe"));
  await fs.symlink(os.tmpdir(), path.join(root, "Documents", "elsewhere"));
  const files = new FilesTool({ home: root });
  for (const args of [
    { action: "list", path: "~/Documents/safe" },
    { action: "list", path: "~/Documents/elsewhere" },
    { action: "move", path: "notes.txt", to: "~/Documents/safe" },
    { action: "new_folder", path: "~/Documents/safe", name: "x" },
    { action: "find", path: "~/Documents/safe", query: "id" },
  ]) {
    assert.equal((await problem(files.prepare(args))).status, "refused", JSON.stringify(args));
  }
  // The link itself is not moved or renamed either.
  assert.equal((await problem(files.prepare({ action: "rename", path: "~/Documents/safe", name: "fine" }))).status, "refused");
  assert.equal(await fs.readFile(path.join(root, ".ssh", "id_ed25519"), "utf8"), "secret");
  await fs.access(path.join(root, "Downloads", "notes.txt"));
});

test("a file that appears while the person decides is never replaced", async () => {
  const root = await home();
  const files = new FilesTool({ home: root });
  const rename = await files.prepare({ action: "rename", path: "notes.txt", name: "todo.txt" });
  const move = await files.prepare({ action: "move", path: "report.pdf", to: "Projects" });
  // Meanwhile, something else creates both names.
  await fs.writeFile(path.join(root, "Downloads", "todo.txt"), "someone else's");
  await fs.writeFile(path.join(root, "Documents", "Projects", "report.pdf"), "a different report");
  assert.equal((await problem(rename.run())).status, "refused");
  assert.equal((await problem(move.run())).status, "refused");
  assert.equal(await fs.readFile(path.join(root, "Downloads", "todo.txt"), "utf8"), "someone else's");
  assert.equal(await fs.readFile(path.join(root, "Documents", "Projects", "report.pdf"), "utf8"), "a different report");
  assert.equal(await fs.readFile(path.join(root, "Downloads", "notes.txt"), "utf8"), "txt");
  assert.equal(await fs.readFile(path.join(root, "Downloads", "report.pdf"), "utf8"), "pdf");
});

test("folders move without replacing, and a change of case is still a rename", async () => {
  const root = await home();
  await fs.mkdir(path.join(root, "Desktop", "Old stuff"));
  await fs.writeFile(path.join(root, "Desktop", "Old stuff", "a.txt"), "a");
  const files = new FilesTool({ home: root });
  const move = await files.prepare({ action: "move", path: "~/Desktop/Old stuff", to: "Documents" });
  await move.run();
  assert.equal(await fs.readFile(path.join(root, "Documents", "Old stuff", "a.txt"), "utf8"), "a");
  const recase = await files.prepare({ action: "rename", path: "notes.txt", name: "Notes.txt" });
  await recase.run();
  assert.deepEqual((await fs.readdir(path.join(root, "Downloads"))).filter((n) => n.toLowerCase() === "notes.txt"), ["Notes.txt"]);
});

test("when Finder refuses, GNSIS says so instead of quietly using another folder", async () => {
  const root = await home();
  const refused = new ActionProblem("needs_permission", "macOS has not let GNSIS control Finder.", { permission: "automation", app: "Finder" });
  const finder: FinderBridge = {
    selection: async () => { throw refused; },
    frontFolder: async () => { throw refused; },
  };
  const files = new FilesTool({ home: root, finder });
  // Where to list or make a folder depended on the open window: reported.
  assert.equal((await problem(files.prepare({ action: "list" }))).status, "needs_permission");
  assert.equal((await problem(files.prepare({ action: "new_folder", name: "X" }))).status, "needs_permission");
  // A name that is in the usual folders is still found without Finder.
  const found = await files.prepare({ action: "move", path: "report.pdf", to: "Projects" });
  assert.equal(found.action, "move");
  // A name that is not: the refusal is what the person hears, not "not found".
  assert.equal((await problem(files.prepare({ action: "move", path: "report.pdf", to: "Taxes" }))).status, "needs_permission");
});

test("cloud folders reached through a link are the person's own files; the rest of Library is not", async () => {
  const root = await home();
  await fs.mkdir(path.join(root, "Library", "CloudStorage", "Dropbox"), { recursive: true });
  await fs.writeFile(path.join(root, "Library", "CloudStorage", "Dropbox", "plan.txt"), "plan");
  await fs.symlink(path.join(root, "Library", "CloudStorage", "Dropbox"), path.join(root, "Dropbox"));
  await fs.mkdir(path.join(root, "Library", "Keychains"), { recursive: true });
  await fs.symlink(path.join(root, "Library", "Keychains"), path.join(root, "Documents", "keys"));
  const files = new FilesTool({ home: root });
  const listed = await (await files.prepare({ action: "list", path: "~/Dropbox" })).run();
  assert.equal(listed.detail?.count, 1);
  const move = await files.prepare({ action: "move", path: "report.pdf", to: "~/Dropbox" });
  await move.run();
  await fs.access(path.join(root, "Library", "CloudStorage", "Dropbox", "report.pdf"));
  assert.equal((await problem(files.prepare({ action: "list", path: "~/Documents/keys" }))).status, "refused");
});
