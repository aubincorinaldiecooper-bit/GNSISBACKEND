/**
 * End to end, minus the neural model: the runtime asks this desktop to do
 * something, the desktop really does it, and the answer reaches the same model
 * session, which then speaks from it.
 *
 *   scripted model ─tool.call─▶ runtime ─(call_id)─▶ HostSession ─▶ ActionBroker
 *        ▲                                                            │ policy
 *        └──── speaks from it ◀─ runtime feeds it ◀─ tool.response ◀──┘ ToolRegistry → real action
 *
 * Uses the desktop's real HostSession, ActionBroker, ToolRegistry and action
 * adapters (the same modules main.ts bundles), against the real runtime app
 * serving runtime/gnsis_runtime/tests/scripted_runtime.py. Then reads the
 * session timeline the runtime wrote and prints the chain for the call.
 *
 * E2E_RUNTIME_URL   runtime to connect to (http://127.0.0.1:18765)
 * E2E_MEDIA_DIR     the runtime's --media-dir, where the timeline lands
 * E2E_SCENARIO      files-move (default) | files-move-asked | files-move-spoken | files-move-typed | open-app
 *                   files-move-spoken: no words are given; the person "speaks" (tone
 *                   audio), the desktop transcribes it through the runtime's own
 *                   /api/asr/transcribe (a stand-in speech-to-text behind it), sends
 *                   turn.final, and the move runs without asking because of it.
 *                   files-move-typed: the person types the request; the desktop's
 *                   TypedTurns sends it as turn.final, the runtime accepts it, and the
 *                   move runs without asking because of it. The scripted model acts on
 *                   audio, not on the words, so this proves the typed turn's delivery
 *                   and its standing with the action policy — not that the real model
 *                   read it.
 * E2E_HOME          a scratch home folder for the files scenarios
 */
import assert from "node:assert/strict";
import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import { HostSession } from "../src/host/hostSession.js";
import { ActionBroker, type ConfirmRequest } from "../src/host/actionBroker.js";
import { TurnLog } from "../src/host/turns.js";
import { TypedTurns } from "../src/host/typedTurns.js";
import { runtimeTranscriber, UtteranceTranscriber } from "../src/host/utterances.js";
import { ToolRegistry } from "../src/tools/registry.js";
import { HOST_TOOL_SCHEMAS, HOST_TOOLS_VERSION } from "../src/tools/catalog.js";
import { FilesTool } from "../src/tools/files.js";
import { OpenTool } from "../src/tools/mac/open.js";
import { MacFinder } from "../src/tools/mac/finder.js";
import { systemShell } from "../src/tools/mac/shell.js";
import type { ClientControl } from "../src/shared/protocol.js";

const RUNTIME = process.env.E2E_RUNTIME_URL ?? "http://127.0.0.1:18765";
const MEDIA_DIR = process.env.E2E_MEDIA_DIR ?? "";
const SCENARIO = process.env.E2E_SCENARIO ?? "files-move";
const started = Date.now();
const t = () => `+${String(Date.now() - started).padStart(5)} ms`;
const say = (line: string) => console.log(`${t()}  ${line}`);

async function main(): Promise<void> {
  const home = process.env.E2E_HOME ?? (await fs.mkdtemp(path.join(os.tmpdir(), "gnsis-e2e-home-")));
  if (SCENARIO.startsWith("files")) {
    await fs.mkdir(path.join(home, "Downloads"), { recursive: true });
    await fs.mkdir(path.join(home, "Documents", "Projects"), { recursive: true });
    await fs.writeFile(path.join(home, "Downloads", "report.pdf"), "a real file");
  }

  const registry = new ToolRegistry({ runtimeUrl: RUNTIME });
  const finder = process.platform === "darwin" && SCENARIO === "open-app" ? new MacFinder(systemShell) : undefined;
  const files = new FilesTool({ home, finder });
  registry.registerAction(files);
  registry.registerAction(new OpenTool(systemShell, files));
  const offered = registry.actionNames(process.platform, HOST_TOOL_SCHEMAS.map((tool) => tool.name));
  say(`desktop offers: ${offered.join(", ")} (catalog ${HOST_TOOLS_VERSION}, platform ${process.platform})`);

  const turns = new TurnLog();
  if (SCENARIO === "files-move") {
    // The person's own words, as a transcriber on this machine would have
    // sent them: the move is covered, so it runs without a second ask.
    turns.record({ turnId: "turn-1", text: "Move that report into the Projects folder.", endedAtMs: Date.now() });
  }
  const asked: ConfirmRequest[] = [];
  const controls: Array<Record<string, unknown>> = [];
  const sent: Array<Record<string, unknown>> = [];

  let broker: ActionBroker | null = null;
  let utterances: UtteranceTranscriber | null = null;
  let typed: TypedTurns | null = null;
  const host = new HostSession({
    runtimeUrl: RUNTIME,
    hostId: "e2e-host",
    chassis: "e2e",
    capabilities: { mic: true, camera: false, screen: false, playback_ack: true, global_shortcuts: false, notifications: false },
    hostTools: { names: offered, version: HOST_TOOLS_VERSION },
    onControl: (c) => {
      const control = c as Record<string, unknown>;
      controls.push(control);
      broker?.handleControl(control);
      if (utterances?.handleControl(control)) say(`runtime → desktop  turn ${String(control.turn_id)} accepted`);
      if (typed?.handleControl(control)) say(`runtime → desktop  typed turn ${String(control.turn_id)} accepted`);
      if (control.type === "tool.call") say(`runtime → desktop  tool.call ${String(control.call_id)} ${JSON.stringify(control.tool_calls)}`);
      if (control.type === "tool.response.queued") say(`runtime → desktop  answer queued for the model (${String(control.call_id)})`);
      if (control.type === "chunk" && control.text) say(`model says: "${String(control.text)}"`);
    },
  });
  broker = new ActionBroker({
    registry,
    send: (control) => {
      sent.push(control);
      if (control.type === "tool.response") say(`desktop → runtime  tool.response ${String(control.call_id)} ${JSON.stringify(control.content)}`);
      host.sendControl(control as unknown as ClientControl);
    },
    event: (event) => host.emit(event),
    confirm: async (request) => {
      asked.push(request);
      say(`desktop asks the person: "Allow GNSIS to ${request.summary}?" — ${request.why} → Allow`);
      return true;
    },
    accessibility: () => true,
    latestTurn: () => turns.latest(),
    waitForWords: () => utterances?.settled() ?? Promise.resolve(),
    log: (area, line) => say(`[${area}] ${line}`),
  });
  if (SCENARIO === "files-move-spoken") {
    utterances = new UtteranceTranscriber({
      transcribe: runtimeTranscriber(RUNTIME),
      send: (control) => {
        if (control.type === "turn.final") say(`desktop → runtime  turn.final "${String(control.text)}"`);
        host.sendControl(control as unknown as ClientControl);
      },
      turns,
      log: (area, line) => say(`[${area}] ${line}`),
    });
  }
  if (SCENARIO === "files-move-typed") {
    typed = new TypedTurns({
      send: (control) => {
        say(`desktop → runtime  turn.final (typed) "${String(control.text)}"`);
        host.sendControl(control as unknown as ClientControl);
      },
      turns,
      ready: () => controls.some((c) => c.type === "ready"),
      log: (area, line) => say(`[${area}] ${line}`),
    });
  }
  const mic = new Mic(host, utterances);

  host.connect(`e2e-${process.pid}`);
  host.ready();
  const ready = await waitFor(() => controls.find((c) => c.type === "ready"), "ready");
  const agreed = (ready.host_tools as { accepted: string[] }).accepted;
  say(`runtime ready; agreed tools: ${agreed.join(", ")}; model tools: ${(ready.tools as string[]).join(", ")}`);
  assert.deepEqual(agreed, offered, "the runtime agrees to exactly what this desktop offered");

  if (typed) {
    // The person types before GNSIS acts; the runtime must confirm it.
    const result = await typed.send("Move that report into the Projects folder.");
    assert.ok(result.ok, result.ok ? "" : result.reason);
  }

  // Microphone audio: the scripted model takes its turn on the first second.
  if (SCENARIO === "files-move-spoken") {
    mic.send(1_500, 0.2); // the person speaks
    mic.send(1_000, 0); //   and stops
  } else {
    mic.send(1_000, 0);
  }
  const call = await waitFor(() => controls.find((c) => c.type === "tool.call" && c.call_id), "tool.call");
  const callId = String(call.call_id);
  const answer = await waitFor(() => sent.find((c) => c.type === "tool.response" && c.call_id === callId), "tool.response");
  await waitFor(() => controls.find((c) => c.type === "tool.response.queued" && c.call_id === callId), "tool.response.queued");
  // The model takes the answer in with the next second of audio.
  mic.send(1_000, 0);
  const spoken = await waitFor(() => controls.find((c) => c.type === "chunk" && typeof c.text === "string" && c.text), "the model's reply");
  const content = answer.content as { status: string; message: string };

  // What must be true on the machine, and in what the model said.
  if (SCENARIO.startsWith("files")) {
    await fs.access(path.join(home, "Documents", "Projects", "report.pdf"));
    await assert.rejects(fs.access(path.join(home, "Downloads", "report.pdf")));
    say(`disk: ${path.join(home, "Documents", "Projects", "report.pdf")} exists, and the original is gone`);
    assert.equal(content.status, "done");
    assert.equal(asked.length, SCENARIO === "files-move-asked" ? 1 : 0, "asked the person exactly when the policy says to");
  }
  if (SCENARIO === "files-move-spoken" || SCENARIO === "files-move-typed") {
    assert.equal(turns.latest()?.text, "Move that report into the Projects folder.", "the person's words came back as a trusted turn");
    say(`trusted turn on record: "${turns.latest()?.text}" (${turns.latest()?.turnId})`);
  }
  if (SCENARIO === "open-app") {
    assert.equal(content.status, "done", content.message);
  }
  assert.ok(String(spoken.text).includes(content.message), "the model spoke from the desktop's answer");

  host.disconnect("e2e_done");
  if (MEDIA_DIR) await printTimeline(callId);
  say(`PASS ${SCENARIO}: ${content.message}`);
}

/** The microphone: 100 ms frames to the runtime, and to the transcriber when there is one. */
class Mic {
  private seq = 0;
  private sample = 0;
  constructor(
    private readonly host: HostSession,
    private readonly words: UtteranceTranscriber | null,
  ) {}

  send(ms: number, amplitude: number): void {
    for (let done = 0; done < ms; done += 100) {
      const pcm = Buffer.alloc(1_600 * 2);
      for (let i = 0; i < 1_600; i += 1) pcm.writeInt16LE(Math.round(amplitude * 32_767 * Math.sin(i / 3)), i * 2);
      this.seq += 1;
      const header = { type: "audio.frame" as const, sequence: this.seq, start_sample: this.sample, sample_count: 1_600, captured_at_ms: Date.now() + done };
      this.sample += 1_600;
      this.host.sendAudioFrame(header, pcm);
      this.words?.feed(header, pcm);
    }
  }
}

async function printTimeline(callId: string): Promise<void> {
  const deadline = Date.now() + 5_000;
  let lines: string[] = [];
  while (Date.now() < deadline) {
    const names = (await fs.readdir(MEDIA_DIR).catch(() => [] as string[])).filter((n) => n.endsWith(".timeline.jsonl"));
    lines = [];
    for (const name of names) lines.push(...(await fs.readFile(path.join(MEDIA_DIR, name), "utf8")).split("\n").filter(Boolean));
    const kinds = lines.map((l) => JSON.parse(l) as { kind: string; correlation_id: string | null }).filter((e) => e.correlation_id === callId).map((e) => e.kind);
    if (kinds.includes("tool.response.injected") && kinds.includes("action.completed")) break;
    await new Promise((r) => setTimeout(r, 100));
  }
  const events = lines
    .map((l) => JSON.parse(l) as { seq: number; kind: string; component: string; correlation_id: string | null; recv_ts_ms: number; fields: Record<string, unknown> })
    .filter((e) => e.correlation_id === callId)
    .sort((a, b) => a.seq - b.seq);
  console.log(`\nSession timeline for ${callId} (as the runtime recorded it):`);
  const first = events[0]?.recv_ts_ms ?? 0;
  for (const e of events) {
    const keep = ["tool", "action", "effect", "provenance", "decision", "reason", "turn_id", "status", "verified", "latency_ms"];
    const fields = Object.fromEntries(Object.entries(e.fields).filter(([k, v]) => keep.includes(k) && v !== undefined && v !== null));
    console.log(`  #${String(e.seq).padStart(3)} +${String(e.recv_ts_ms - first).padStart(4)} ms  ${e.component.padEnd(5)} ${e.kind.padEnd(24)} ${JSON.stringify(fields)}`);
  }
  const kinds = events.map((e) => e.kind);
  for (const needed of ["tool.requested", "action.requested", "action.policy", "action.started", "action.completed", "tool.response.received", "tool.response.injected"]) {
    assert.ok(kinds.includes(needed), `timeline is missing ${needed}`);
  }
}

async function waitFor<T>(find: () => T | undefined, what: string, ms = 15_000): Promise<NonNullable<T>> {
  const deadline = Date.now() + ms;
  while (Date.now() < deadline) {
    const found = find();
    if (found) return found as NonNullable<T>;
    await new Promise((r) => setTimeout(r, 20));
  }
  throw new Error(`timed out waiting for ${what}`);
}

main().then(
  () => process.exit(0),
  (err) => {
    console.error(`${t()}  FAIL ${SCENARIO}: ${err instanceof Error ? err.stack : String(err)}`);
    process.exit(1);
  },
);
