import assert from "node:assert/strict";
import { test } from "node:test";
import { localCommand } from "./localCommands.js";

test("direct app launches use the local open tool", () => {
  assert.deepEqual(localCommand("Open Calculator"), {
    tool: "open",
    args: { target: "Calculator" },
  });
  assert.deepEqual(localCommand("please launch FaceTime please"), {
    tool: "open",
    args: { target: "FaceTime" },
  });
});

test("multi-step and ambiguous requests stay with the foreground model", () => {
  assert.equal(localCommand("Open Notes and draft a letter"), null);
  assert.equal(localCommand("Organize my desktop"), null);
  assert.equal(localCommand("Answer my messages"), null);
  assert.equal(localCommand(""), null);
});
