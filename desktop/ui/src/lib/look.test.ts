import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { applyLook, loadLook, saveLook } from "./look";

const g = globalThis as { localStorage?: unknown };
afterEach(() => { delete g.localStorage; });

function storage(broken = false) {
  const m = new Map<string, string>();
  g.localStorage = {
    getItem: (k: string) => { if (broken) throw new Error("off"); return m.get(k) ?? null; },
    setItem: (k: string, v: string) => { if (broken) throw new Error("off"); m.set(k, v); },
  };
}

test("starts clear and remembers dark", () => {
  storage();
  assert.equal(loadLook(), "clear");
  saveLook("dark");
  assert.equal(loadLook(), "dark");
  saveLook("clear");
  assert.equal(loadLook(), "clear");
});

test("storage that is switched off falls back to clear", () => {
  storage(true);
  saveLook("dark");
  assert.equal(loadLook(), "clear");
});

test("only the dark look marks the page", () => {
  const set = new Set<string>();
  const target = { classList: { toggle: (n: string, on: boolean) => (on ? set.add(n) : set.delete(n)) } };
  applyLook("dark", target);
  assert.ok(set.has("gnsis-dark"));
  applyLook("clear", target);
  assert.ok(!set.has("gnsis-dark"));
});
