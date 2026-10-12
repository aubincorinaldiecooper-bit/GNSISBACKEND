import { test } from "node:test";
import assert from "node:assert/strict";
import { clipProblem, peaksOf, uploadReason, MAX_VOICE_BYTES } from "./VoiceUpload";
import { stageLayout, CHAT_H } from "./Stage";

test("a picked file outside the server's limits is turned down before uploading", () => {
  assert.equal(clipProblem(MAX_VOICE_BYTES + 1, 10), "This file is larger than 5 MB.");
  assert.match(clipProblem(1000, null)!, /isn’t a WAV recording/);
  assert.match(clipProblem(1000, 2)!, /2 seconds\. It needs to be 3 to 30 seconds/);
  assert.match(clipProblem(1000, 31)!, /3 to 30 seconds/);
  assert.equal(clipProblem(1000, 3), null);
  assert.equal(clipProblem(MAX_VOICE_BYTES, 30), null);
});

test("the waveform has one bar per slice, the loudest at full height", () => {
  const samples = new Float32Array(400);
  samples[50] = -0.5;
  samples[350] = 0.25;
  const peaks = peaksOf(samples, 4);
  assert.deepEqual(peaks, [1, 0, 0, 0.5]);
});

test("a failed upload shows the server's reason as a sentence", () => {
  assert.equal(
    uploadReason(new Error("Error invoking remote method 'voice:profile:upload': Error: reference audio must be 3 to 30 seconds")),
    "Reference audio must be 3 to 30 seconds.",
  );
  assert.equal(uploadReason(new Error("")), "The voice could not be uploaded.");
});

test("the chat is one fixed height and the drawer runs from its top to the bar's bottom", () => {
  for (const [w, h] of [[1440, 900], [1920, 1080], [900, 900]] as const) {
    const l = stageLayout(w, h);
    assert.equal(l.chatH, CHAT_H);
    assert.equal(l.drawerH, CHAT_H + 14 + 68);
  }
  assert.equal(stageLayout(1440, 600).chatH, 600 - 236);
});
