import assert from "node:assert/strict";
import { test } from "node:test";
import { actionsAllowed } from "./runtimeTrust.js";

test("actions are offered to GNSIS's own site over TLS and to a runtime on this machine", () => {
  assert.equal(actionsAllowed("https://gnsis.studio", undefined).allowed, true);
  assert.equal(actionsAllowed("https://api.gnsis.studio", undefined).allowed, true);
  assert.equal(actionsAllowed("http://127.0.0.1:8080", undefined).allowed, true);
  assert.equal(actionsAllowed("http://localhost:7975", undefined).allowed, true);
});

test("anything else gets no actions unless the person turned them on", () => {
  assert.equal(actionsAllowed("http://gnsis.studio", undefined).allowed, false, "not over TLS");
  assert.equal(actionsAllowed("https://gnsis.studio.evil.example", undefined).allowed, false);
  assert.equal(actionsAllowed("https://example.com", undefined).allowed, false);
  assert.equal(actionsAllowed("https://example.com", "on").allowed, true);
  assert.equal(actionsAllowed("https://gnsis.studio", "off").allowed, false);
});
