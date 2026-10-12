import assert from "node:assert/strict";
import test from "node:test";
import { dispatchWakeGesture } from "./platform";

test("double tap starts or toggles voice; single tap only finishes it", () => {
  const actions: string[] = [];
  const dispatch = (kind: unknown) =>
    dispatchWakeGesture(
      kind,
      () => actions.push("toggle"),
      () => actions.push("finish"),
    );
  dispatch("double");
  dispatch("single");
  dispatch("triple");
  assert.deepEqual(actions, ["toggle", "finish"]);
});
