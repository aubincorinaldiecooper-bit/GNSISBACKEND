import assert from "node:assert/strict";
import { test } from "node:test";
import { TurnLog } from "./turns.js";
import { runtimeTranscriber, TranscriberUnavailable, UtteranceTranscriber } from "./utterances.js";
import type { AudioFrameHeader } from "../shared/protocol.js";

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** Feed `ms` of audio at a given amplitude in 100 ms frames. */
function speak(t: UtteranceTranscriber, clock: { ms: number; seq: number }, ms: number, amplitude: number): void {
  for (let done = 0; done < ms; done += 100) {
    const pcm = Buffer.alloc(1600 * 2);
    for (let i = 0; i < 1600; i += 1) pcm.writeInt16LE(Math.round(amplitude * 32767 * Math.sin(i / 3)), i * 2);
    const header: AudioFrameHeader = { type: "audio.frame", sequence: ++clock.seq, start_sample: 0, sample_count: 1600, captured_at_ms: clock.ms };
    t.feed(header, pcm);
    clock.ms += 100;
  }
}

function setup(transcribe: (pcm: Buffer, start: number) => Promise<string>) {
  const sent: Array<Record<string, unknown>> = [];
  const logs: string[] = [];
  const turns = new TurnLog();
  const heard: Buffer[] = [];
  const t = new UtteranceTranscriber({
    transcribe: async (pcm, start) => {
      heard.push(pcm);
      return transcribe(pcm, start);
    },
    send: (c) => sent.push(c),
    turns,
    log: (_a, line) => logs.push(line),
  });
  return { t, sent, logs, turns, heard, clock: { ms: 1_700_000_000_000, seq: 0 } };
}

test("an utterance is transcribed, sent as turn.final, and recorded once the runtime accepts it", async () => {
  const s = setup(async () => "Move that file into the Projects folder.");
  speak(s.t, s.clock, 1_000, 0);
  speak(s.t, s.clock, 1_500, 0.2);
  speak(s.t, s.clock, 1_000, 0);
  await sleep(10);
  assert.equal(s.heard.length, 1, "one utterance");
  const seconds = s.heard[0].length / 2 / 16_000;
  assert.ok(seconds >= 1.5 && seconds <= 2.6, `utterance length ${seconds}s includes pre-roll and the quiet that ended it`);
  const turn = s.sent.find((c) => c.type === "turn.final")!;
  assert.equal(turn.text, "Move that file into the Projects folder.");
  assert.equal(s.turns.latest(), null, "not trusted until the runtime accepts it");
  assert.equal(s.t.handleControl({ type: "turn.final.accepted", turn_id: turn.turn_id }), true);
  assert.equal(s.turns.latest()?.text, "Move that file into the Projects folder.");
  assert.equal(s.turns.latest()?.turnId, turn.turn_id);
});

test("silence and a short noise make no turn", async () => {
  const s = setup(async () => "should not be asked");
  speak(s.t, s.clock, 2_000, 0.001);
  speak(s.t, s.clock, 200, 0.3);
  speak(s.t, s.clock, 1_000, 0);
  await sleep(10);
  assert.equal(s.heard.length, 0);
  assert.equal(s.sent.length, 0);
});

test("with no speech-to-text at the runtime, it says so once and stops asking for a while", async () => {
  const s = setup(async () => {
    throw new TranscriberUnavailable("HTTP 503");
  });
  for (let i = 0; i < 3; i += 1) {
    speak(s.t, s.clock, 1_000, 0.2);
    speak(s.t, s.clock, 1_000, 0);
    await sleep(5);
  }
  assert.equal(s.heard.length, 1, "asked once, then backed off");
  assert.equal(s.logs.filter((l) => l.includes("no speech-to-text")).length, 1);
  assert.equal(s.turns.latest(), null);
});

test("an action waits while the person is still talking, and until their words are in", async () => {
  let release: (text: string) => void = () => {};
  const s = setup(() => new Promise((r) => (release = r)));
  speak(s.t, s.clock, 1_000, 0.2);
  let settled = false;
  const waiting = s.t.settled(2_000).then(() => (settled = true));
  await sleep(5);
  assert.equal(settled, false, "still talking");
  speak(s.t, s.clock, 1_000, 0);
  await sleep(5);
  assert.equal(settled, false, "transcribing");
  release("open github");
  await sleep(5);
  const turn = s.sent.find((c) => c.type === "turn.final")!;
  s.t.handleControl({ type: "turn.final.accepted", turn_id: turn.turn_id });
  await waiting;
  assert.equal(s.turns.latest()?.text, "open github");
});

test("the runtime's transcriber: 503 means none here; words come back as text", async () => {
  const unavailable = runtimeTranscriber("https://gnsis.studio", (async () => new Response("", { status: 503 })) as typeof fetch);
  await assert.rejects(unavailable(Buffer.alloc(10), 0), TranscriberUnavailable);
  let asked = "";
  const working = runtimeTranscriber("https://gnsis.studio", (async (url: URL) => {
    asked = String(url);
    return new Response(JSON.stringify({ text: "hello" }), { status: 200 });
  }) as unknown as typeof fetch);
  assert.equal(await working(Buffer.alloc(10), 1234), "hello");
  assert.equal(asked, "https://gnsis.studio/api/asr/transcribe?start_ms=1234");
});
