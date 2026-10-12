import assert from "node:assert/strict";
import { test } from "node:test";
import {
  LocalController,
  type LocalControllerDecision,
} from "./localController.js";

test("a bounded local controller runs multiple actions through the broker", async () => {
  const decisions: LocalControllerDecision[] = [
    {
      kind: "tool",
      tool: "files",
      args: { action: "new_folder", path: "Desktop", name: "Receipts" },
      confidence: 0.91,
    },
    {
      kind: "tool",
      tool: "files",
      args: { action: "move", path: "selected", to: "Receipts" },
      confidence: 0.88,
    },
    { kind: "done", message: "I made Receipts and moved the selected file." },
  ];
  const recorded: unknown[] = [];
  const calls: unknown[] = [];
  const answers: string[] = [];
  const controller = new LocalController({
    provider: {
      decide: async () => decisions.shift()!,
    },
    tools: ["files"],
    record: (turn) => recorded.push(turn),
    runLocal: async (...call) => {
      calls.push(call);
      return { status: "done", message: "Done." };
    },
    answer: (text) => answers.push(text),
    now: () => 100,
  });

  assert.deepEqual(
    await controller.send(
      "Create a Receipts folder on my desktop and move the selected file into it",
    ),
    { ok: true, turnId: "local-gnsis05-100-1" },
  );
  assert.deepEqual(recorded, [
    {
      turnId: "local-gnsis05-100-1",
      text: "Create a Receipts folder on my desktop and move the selected file into it",
      endedAtMs: 100,
    },
  ]);
  assert.deepEqual(calls, [
    [
      "gnsis05-100-1-call-1",
      "local-gnsis05-100-1",
      "files",
      { action: "new_folder", path: "Desktop", name: "Receipts" },
    ],
    [
      "gnsis05-100-1-call-2",
      "local-gnsis05-100-1",
      "files",
      { action: "move", path: "selected", to: "Receipts" },
    ],
  ]);
  assert.deepEqual(answers, [
    "I made Receipts and moved the selected file.",
  ]);
});

test("abstention before an action leaves the request for cloud fallback", async () => {
  const controller = new LocalController({
    provider: {
      decide: async () => ({ kind: "fallback", reason: "too broad" }),
    },
    tools: ["files"],
    record: () => assert.fail("an abstained request is not a trusted local turn"),
    runLocal: async () => assert.fail("an abstained request must not execute"),
  });

  assert.equal(await controller.send("Research the best laptop for me"), null);
});

test("an unavailable model before any action leaves the request for cloud fallback", async () => {
  const controller = new LocalController({
    provider: {
      decide: async () => {
        throw new Error("offline");
      },
    },
    tools: ["files"],
    record: () => assert.fail("an unavailable model is not a trusted local turn"),
    runLocal: async () => assert.fail("an unavailable model must not execute"),
  });

  assert.equal(await controller.send("Organize my desktop"), null);
});

test("invalid and partial local decisions fail closed", async () => {
  const decisions: LocalControllerDecision[] = [
    {
      kind: "tool",
      tool: "files",
      args: { action: "list", path: "Desktop" },
      confidence: 0.9,
    },
    { kind: "fallback", reason: "I am no longer certain." },
  ];
  const controller = new LocalController({
    provider: { decide: async () => decisions.shift()! },
    tools: ["files"],
    record: () => {},
    runLocal: async () => ({ status: "done", message: "Listed Desktop." }),
  });

  assert.deepEqual(await controller.send("Organize my desktop"), {
    ok: false,
    reason: "I am no longer certain.",
  });

  const invalid = new LocalController({
    provider: {
      decide: async () => ({
        kind: "tool",
        tool: "shell",
        args: {},
        confidence: 0.99,
      }),
    },
    tools: ["files"],
    record: () => assert.fail("an invalid decision must not become trusted"),
    runLocal: async () => assert.fail("an invalid tool must not execute"),
  });
  assert.deepEqual(await invalid.send("Run this command"), {
    ok: false,
    reason: "GNSIS 0.5 proposed an action this Mac did not offer.",
  });
});

test("the controller stops a runaway local loop", async () => {
  let calls = 0;
  const controller = new LocalController({
    provider: {
      decide: async () => ({
        kind: "tool",
        tool: "files",
        args: { action: "list", path: "Desktop" },
        confidence: 0.9,
      }),
    },
    tools: ["files"],
    record: () => {},
    runLocal: async () => {
      calls += 1;
      return { status: "done", message: "Listed Desktop." };
    },
    maxSteps: 2,
  });

  assert.deepEqual(await controller.send("Keep listing forever"), {
    ok: false,
    reason: "GNSIS 0.5 reached its step limit before finishing.",
  });
  assert.equal(calls, 2);
});

test("low-confidence actions abstain before execution", async () => {
  let calls = 0;
  const controller = new LocalController({
    provider: {
      decide: async () => ({
        kind: "tool",
        tool: "files",
        args: { action: "list", path: "Desktop" },
        confidence: 0.51,
      }),
    },
    tools: ["files"],
    record: () =>
      assert.fail("an uncertain decision must not become trusted"),
    runLocal: async () => {
      calls += 1;
      return { status: "done" };
    },
  });

  assert.equal(await controller.send("Organize this"), null);
  assert.equal(calls, 0);
});
