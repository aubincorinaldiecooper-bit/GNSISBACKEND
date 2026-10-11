import assert from "node:assert/strict";
import { test } from "node:test";
import { judge, reasonForLog, saidIn, TURN_FRESH_MS, type TrustedTurn } from "./actionPolicy.js";
import type { PreparedAction } from "../tools/actions.js";

const NOW = 1_800_000_000_000;

function action(partial: Partial<PreparedAction>): PreparedAction {
  return {
    tool: "files",
    action: "move",
    effect: "change",
    summary: "Move “report.pdf” into “Projects”",
    scope: [
      { value: "report.pdf", source: "selection" },
      { value: "Projects", source: "named" },
    ],
    run: async () => ({ verified: "disk", message: "" }),
    ...partial,
  };
}

const said = (text: string, agoMs = 2_000): TrustedTurn => ({ turnId: "t1", text, endedAtMs: NOW - agoMs });

test("moving what the person selected into the folder they named runs without asking", () => {
  const verdict = judge(action({}), said("Move that file into the Projects folder."), NOW);
  assert.equal(verdict.provenance, "direct_user");
  assert.equal(verdict.decision, "allow");
  assert.equal(verdict.turnId, "t1");
});

test("a destination the person never said is asked about", () => {
  const verdict = judge(action({}), said("Tidy this up for me."), NOW);
  assert.equal(verdict.provenance, "mixed");
  assert.equal(verdict.decision, "confirm");
});

test("with no words on record, a change is asked about, never assumed", () => {
  const verdict = judge(action({}), null, NOW);
  assert.equal(verdict.provenance, "unknown");
  assert.equal(verdict.decision, "confirm");
  assert.equal(verdict.turnId, null);
});

test("an old turn does not cover a new action", () => {
  const verdict = judge(action({}), said("move it into Projects", TURN_FRESH_MS + 1), NOW);
  assert.equal(verdict.provenance, "unknown");
  assert.equal(verdict.decision, "confirm");
});

test("looking and opening local things run whoever asked: nothing changes, nothing leaves", () => {
  for (const effect of ["read", "open_local"] as const) {
    const verdict = judge(action({ effect, scope: [{ value: "Spotify", source: "named" }] }), null, NOW);
    assert.equal(verdict.decision, "allow", effect);
  }
});

test("a web address the person did not say is asked about — it could carry what is on screen away", () => {
  const remote = action({ effect: "open_remote", scope: [{ value: "evil.example", source: "named", kind: "site" }] });
  assert.equal(judge(remote, said("open github for me"), NOW).decision, "confirm");
  const asked = action({ effect: "open_remote", scope: [{ value: "github.com", source: "named", kind: "site" }] });
  assert.equal(judge(asked, said("open github for me"), NOW).decision, "allow");
});

test("a site's name opens only its real address; any other address with that name in it is asked about", () => {
  const site = (host: string) => action({ effect: "open_remote", scope: [{ value: host, source: "named", kind: "site" }] });
  const words = said("Search Andrew Tate on YouTube");
  assert.equal(judge(site("youtube.com"), words, NOW).decision, "allow");
  for (const host of ["youtube.lol", "andrew.lol", "attacker.andrew.io", "youtube.com.evil.io"]) {
    assert.equal(judge(site(host), words, NOW).decision, "confirm", host);
  }
  // Spelled out, the same address is the person's own request.
  assert.equal(judge(site("youtube.lol"), said("open youtube dot lol"), NOW).decision, "allow");
});

test("quit, trash, send and buy are always asked about, even when requested", () => {
  const quit = action({ tool: "input", effect: "input", scope: [{ value: "q", source: "named" }], consequential: "cmd+q quits the app in front" });
  const verdict = judge(quit, said("press command q"), NOW);
  assert.equal(verdict.decision, "confirm");
  assert.match(verdict.reason, /always asked/);
});

test("names match the way people say them", () => {
  assert.ok(saidIn("Projects", "put it in my projects folder"));
  assert.ok(saidIn("q3-report.pdf", "rename the Q3 report"));
  assert.ok(!saidIn("Projects", "put it in my documents"));
  assert.ok(!saidIn("", "anything"));
  assert.ok(saidIn("Stack Overflow", "search stackoverflow"), "words in a row still make a name, and one word many");
  assert.ok(saidIn("GitHub", "open git hub"));
});

test("a name has to be the person's words, not a piece of them", () => {
  // Hosts hidden inside "Open YouTube and search Andrew Tate" were never named.
  const words = "Open YouTube and search Andrew Tate";
  for (const host of ["ubeand.com", "you.com", "drewt.net", "earch.org", "ndrewtat.io"]) {
    assert.ok(!saidIn(host, words), `${host} was not named`);
  }
  assert.ok(!saidIn("port.txt", "open the report"), "nor is a file whose name is inside another word");
  // An injected address to one of those hosts is asked about, not run.
  const injected = action({ effect: "open_remote", scope: [{ value: "ubeand.com", source: "named", kind: "site" }] });
  assert.equal(judge(injected, said(words), NOW).decision, "confirm");
});

test("a reason goes into the log without the words it quotes", () => {
  const reason = "always asked: clicking “Send $2,000 to Andrew Smith” may send, buy, delete or commit something";
  assert.equal(reasonForLog(reason), "always asked: clicking “…” may send, buy, delete or commit something");
  assert.equal(reasonForLog("asked for in the person's own words"), "asked for in the person's own words");
});

test("everyday shortcuts are named by what they do, as well as by their key", async () => {
  const { InputTool } = await import("../tools/mac/input.js");
  const cua = { call: async () => ({ text: "ok" }) };
  const input = new InputTool(cua, { bounds: () => ({ x: 0, y: 0, width: 1440, height: 900 }) });
  const verdict = async (keys: string, words: string) => judge(await input.prepare({ action: "keys", keys }), said(words), NOW);
  for (const [keys, words] of [
    ["cmd+c", "copy that"],
    ["cmd+v", "paste it here"],
    ["cmd+s", "save this document"],
    ["cmd+f", "find the word budget"],
    ["cmd+z", "undo that"],
    ["cmd+t", "open a new tab"],
    ["cmd+c", "press command c"],
  ] as const) {
    assert.equal((await verdict(keys, words)).decision, "allow", `${keys} for “${words}”`);
  }
  // A shortcut the person did not ask for, by name or key, is still asked about.
  assert.equal((await verdict("cmd+v", "copy that")).decision, "confirm");
  assert.equal((await verdict("enter", "search for pasta recipes")).decision, "confirm");
  // Quitting is always asked about, however it is named.
  assert.equal((await verdict("cmd+q", "quit, press command q")).decision, "confirm");
});
