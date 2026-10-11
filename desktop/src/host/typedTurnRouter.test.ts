import assert from "node:assert/strict";
import { test } from "node:test";
import { TypedTurnRouter } from "./typedTurnRouter.js";

test("a direct open command records trusted words and never reaches the cloud fallback", async () => {
  const fallback: unknown[] = [];
  const recorded: unknown[] = [];
  const local: unknown[] = [];
  const router = new TypedTurnRouter({
    fallback: async (input) => {
      fallback.push(input);
      return { ok: false, reason: "offline" };
    },
    record: (turn) => recorded.push(turn),
    runLocal: async (...call) => {
      local.push(call);
      return { status: "done", message: "Calculator is open." };
    },
    now: () => 100,
  });

  assert.deepEqual(await router.send(" Open Calculator "), {
    ok: true,
    turnId: "local-100-1",
  });
  assert.deepEqual(fallback, []);
  assert.deepEqual(recorded, [
    {
      turnId: "local-100-1",
      text: "Open Calculator",
      endedAtMs: 100,
    },
  ]);
  assert.deepEqual(local, [
    [
      "local-call-100-1",
      "local-100-1",
      "open",
      { target: "Calculator" },
    ],
  ]);
});

test("reasoning-heavy text remains cloud-bound and preserves its original input", async () => {
  const fallback: unknown[] = [];
  const router = new TypedTurnRouter({
    fallback: async (input) => {
      fallback.push(input);
      return { ok: true, turnId: "typed-cloud" };
    },
    record: () => assert.fail("a cloud turn must not be trusted locally"),
    runLocal: async () => {
      assert.fail("a complex request must not run locally");
    },
  });

  assert.deepEqual(await router.send("Open Notes and draft a letter"), {
    ok: true,
    turnId: "typed-cloud",
  });
  assert.deepEqual(fallback, ["Open Notes and draft a letter"]);
});

test("a local execution failure is reported without silently retrying in the cloud", async () => {
  const router = new TypedTurnRouter({
    fallback: async () => {
      assert.fail("a matched local command must not run twice through the cloud");
    },
    record: () => {},
    runLocal: async () => ({
      status: "not_found",
      message: "There is no app called Abacus on this Mac.",
    }),
  });

  assert.deepEqual(await router.send("Open Abacus"), {
    ok: false,
    reason: "There is no app called Abacus on this Mac.",
  });
});
