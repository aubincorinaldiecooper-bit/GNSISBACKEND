import assert from "node:assert/strict";
import { test } from "node:test";
import { TurnLog } from "./turns.js";
import { MAX_TYPED_CHARS, TypedTurns } from "./typedTurns.js";

function harness(opts: { ready?: boolean; acceptTimeoutMs?: number } = {}) {
  const sent: Array<Record<string, unknown>> = [];
  const logs: string[] = [];
  const turns = new TurnLog();
  let ready = opts.ready ?? true;
  const typed = new TypedTurns({
    send: (control) => sent.push(control),
    turns,
    ready: () => ready,
    log: (area, line) => logs.push(`${area}: ${line}`),
    now: () => 1_790_000_000_000,
    acceptTimeoutMs: opts.acceptTimeoutMs ?? 50,
  });
  return { typed, sent, logs, turns, setReady: (r: boolean) => (ready = r) };
}

test("a typed message goes to the runtime as the person's turn.final, and is trusted once accepted", async () => {
  const { typed, sent, turns, logs } = harness();
  const pending = typed.send("  Open YouTube and search Andrew Tate ");
  assert.equal(sent.length, 1);
  const control = sent[0];
  assert.equal(control.type, "turn.final");
  assert.equal(control.text, "Open YouTube and search Andrew Tate");
  assert.equal(control.timestamp_ms, 1_790_000_000_000);
  assert.equal(typeof control.timezone, "string");
  assert.ok(!("start_ms" in control) && !("end_ms" in control), "a typed turn claims no span of audio");
  assert.equal(turns.latest(), null, "nothing is trusted before the runtime accepts it");

  assert.equal(typed.handleControl({ type: "turn.final.accepted", turn_id: control.turn_id, context_revision: 1 }), true);
  assert.deepEqual(await pending, { ok: true, turnId: control.turn_id });
  assert.deepEqual(turns.latest(), {
    turnId: control.turn_id,
    text: "Open YouTube and search Andrew Tate",
    endedAtMs: 1_790_000_000_000,
  });
  assert.ok(logs.some((l) => l.includes(`typed turn ${String(control.turn_id)} sent`)));
  assert.ok(logs.some((l) => l.includes(`typed turn ${String(control.turn_id)} accepted`)));
});

test("a turn the runtime never confirms is reported as not delivered, and never trusted", async () => {
  const { typed, turns } = harness({ acceptTimeoutMs: 20 });
  const result = await typed.send("Open YouTube");
  assert.equal(result.ok, false);
  assert.match(!result.ok ? result.reason : "", /didn’t confirm/);
  assert.equal(turns.latest(), null);
});

test("nothing is sent while the runtime is not connected, or when there is nothing to send", async () => {
  const { typed, sent, setReady } = harness({ ready: false });
  assert.deepEqual(await typed.send("Open YouTube"), { ok: false, reason: "GNSIS isn’t connected right now. Try again in a moment." });
  setReady(true);
  assert.equal((await typed.send("   ")).ok, false);
  assert.equal((await typed.send(42)).ok, false);
  assert.equal((await typed.send("x".repeat(MAX_TYPED_CHARS + 1))).ok, false);
  assert.deepEqual(sent, []);
});

test("acceptances for other turns are not ours, and a closed connection abandons what was waiting", async () => {
  const { typed, sent, turns } = harness({ acceptTimeoutMs: 10_000 });
  assert.equal(typed.handleControl({ type: "turn.final.accepted", turn_id: "desk-1-1" }), false, "a spoken turn's acceptance is left to the transcriber");
  const pending = typed.send("Open YouTube");
  assert.equal(sent.length, 1);
  typed.abandonAll();
  const result = await pending;
  assert.equal(result.ok, false);
  assert.match(!result.ok ? result.reason : "", /connection to GNSIS closed/);
  assert.equal(typed.handleControl({ type: "turn.final.accepted", turn_id: sent[0].turn_id }), false, "a late acceptance changes nothing");
  assert.equal(turns.latest(), null);
});
