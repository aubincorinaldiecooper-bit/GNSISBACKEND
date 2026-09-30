import assert from "node:assert/strict";
import { test } from "node:test";
import { PersonApp, appToRestore } from "./personApp.js";

test("the person's own app is remembered, never GNSIS", async () => {
  let front: string | null = "Safari";
  const person = new PersonApp("GNSIS", async () => front);
  await person.noteFront();
  assert.equal(person.name, "Safari");
  front = "GNSIS"; // the person clicked the voice button
  await person.noteFront();
  assert.equal(person.name, "Safari", "GNSIS coming forward does not replace it");
  front = "TextEdit";
  await person.noteFront();
  assert.equal(person.name, "TextEdit");
  front = null; // could not be read
  await person.noteFront();
  assert.equal(person.name, "TextEdit");
  const broken = new PersonApp("GNSIS", async () => { throw new Error("lsappinfo failed"); });
  await broken.noteFront();
  assert.equal(broken.name, null);
});

test("GNSIS puts the person's app back in front only when GNSIS itself is in front", () => {
  assert.equal(appToRestore("GNSIS", "GNSIS", "TextEdit"), "TextEdit", "paste it here: into TextEdit, not GNSIS");
  assert.equal(appToRestore("Safari", "GNSIS", "TextEdit"), null, "someone's app is already in front: leave it");
  assert.equal(appToRestore("GNSIS", "GNSIS", null), null, "no app known: nothing to bring back");
  assert.equal(appToRestore(null, "GNSIS", "TextEdit"), null, "the front app could not be read: do nothing");
});
