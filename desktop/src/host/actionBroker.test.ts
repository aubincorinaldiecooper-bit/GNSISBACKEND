import assert from "node:assert/strict";
import { test } from "node:test";
import { ActionBroker, fit, type ActionBrokerDeps, type ConfirmRequest } from "./actionBroker.js";
import { ToolRegistry } from "../tools/registry.js";
import { ActionProblem, type ActionTool, type PreparedAction } from "../tools/actions.js";
import type { ActionEvent } from "./protocol.js";
import type { TrustedTurn } from "./actionPolicy.js";

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** A files tool that records what it was asked to run. */
class FakeFiles implements ActionTool {
  readonly name = "files";
  readonly platforms = [process.platform] as const;
  runs = 0;
  permission: "ok" | "accessibility" = "ok";
  async prepare(args: Record<string, unknown>): Promise<PreparedAction> {
    if (args.to === "Nowhere") throw new ActionProblem("not_found", "No folder called Nowhere.");
    return {
      tool: "files",
      action: String(args.action),
      effect: args.action === "list" ? "read" : "change",
      summary: `Move “${String(args.path)}” into “${String(args.to)}”`,
      scope: [{ value: String(args.to), source: "named" }],
      needs: this.permission === "accessibility" ? [{ kind: "accessibility" }] : [],
      run: async () => {
        this.runs += 1;
        return { verified: "disk", message: "Moved it.", detail: { to: `~/${String(args.to)}` } };
      },
    };
  }
}

function setup(opts: { turn?: TrustedTurn; confirm?: (r: ConfirmRequest, s: AbortSignal) => Promise<boolean>; trusted?: boolean; look?: "fresh" | "not_shared" | "not_yet"; waitForWords?: () => Promise<void>; latestTurn?: () => TrustedTurn | null } = {}) {
  const registry = new ToolRegistry({ runtimeUrl: "http://127.0.0.1:1" });
  const files = new FakeFiles();
  registry.registerAction(files);
  const sent: Array<Record<string, unknown>> = [];
  const events: ActionEvent[] = [];
  const asked: ConfirmRequest[] = [];
  const prompts: boolean[] = [];
  const updates: string[] = [];
  const logs: string[] = [];
  const deps: ActionBrokerDeps = {
    registry,
    send: (c) => sent.push(c),
    event: (e) => events.push(e),
    confirm: async (request, signal) => {
      asked.push(request);
      return opts.confirm ? opts.confirm(request, signal) : true;
    },
    accessibility: (prompt) => {
      prompts.push(prompt);
      return opts.trusted ?? true;
    },
    latestTurn: opts.latestTurn ?? (() => opts.turn ?? null),
    waitForWords: opts.waitForWords,
    log: (area, line) => logs.push(`${area}: ${line}`),
    notify: (u) => updates.push(u.state),
    lookAfter: opts.look ? async () => opts.look! : undefined,
  };
  const broker = new ActionBroker(deps);
  broker.handleControl({ type: "ready", host_tools: { accepted: ["files"] } });
  return { broker, files, sent, events, asked, prompts, updates, logs };
}

const call = (callId: string, args: Record<string, unknown>, extra: Record<string, unknown> = {}) => ({
  type: "tool.call",
  call_id: callId,
  dispatch: "client",
  tool_calls: [{ name: "files", arguments: args }],
  tool_response_expected: true,
  ...extra,
});

const move = { action: "move", path: "report.pdf", to: "Projects" };

async function answer(sent: Array<Record<string, unknown>>, callId: string) {
  for (let i = 0; i < 100; i += 1) {
    const found = sent.find((c) => c.type === "tool.response" && c.call_id === callId);
    if (found) return found;
    await sleep(5);
  }
  assert.fail(`no tool.response for ${callId}`);
}

test("a requested move runs once and answers with the same call id", async () => {
  const { broker, files, sent, events, asked } = setup({
    turn: { turnId: "t9", text: "move the report into Projects", endedAtMs: Date.now() - 1000 },
  });
  assert.equal(broker.handleControl(call("call_1", move)), true);
  const response = await answer(sent, "call_1");
  assert.deepEqual(response.content, { status: "done", message: "Moved it.", verified: "disk", to: "~/Projects" });
  assert.equal(files.runs, 1);
  assert.equal(asked.length, 0, "the person's own words covered it: no second ask");
  assert.deepEqual(
    events.map((e) => e.type),
    ["action.requested", "action.policy", "action.started", "action.completed"],
  );
  assert.ok(events.every((e) => e.call_id === "call_1"));
  const policy = events.find((e) => e.type === "action.policy")!;
  assert.equal(policy.provenance, "direct_user");
  assert.equal(policy.turn_id, "t9");
});

test("a call delivered again after a reconnect is answered from its result, not run twice", async () => {
  const { broker, files, sent } = setup();
  broker.handleControl(call("call_2", move));
  await answer(sent, "call_2");
  broker.handleControl(call("call_2", move, { redelivered: true }));
  await sleep(20);
  assert.equal(files.runs, 1);
  const answers = sent.filter((c) => c.type === "tool.response" && c.call_id === "call_2");
  assert.equal(answers.length, 2);
  assert.deepEqual(answers[0].content, answers[1].content);
});

test("without the person's words on record, a move waits for their OK and keeps the call alive", async () => {
  let release: (ok: boolean) => void = () => {};
  const { broker, files, sent, asked } = setup({ confirm: () => new Promise((r) => (release = r)) });
  broker.handleControl(call("call_3", move));
  await sleep(20);
  assert.equal(asked.length, 1);
  assert.equal(asked[0].summary, "Move “report.pdf” into “Projects”");
  assert.match(asked[0].why, /couldn’t confirm that you asked/);
  assert.equal(files.runs, 0, "nothing runs while the person decides");
  assert.ok(sent.some((c) => c.type === "tool.progress" && c.call_id === "call_3"));
  release(true);
  const response = await answer(sent, "call_3");
  assert.equal((response.content as { status: string }).status, "done");
  assert.equal(files.runs, 1);
});

test("declined means not done, and the model is told so", async () => {
  const { broker, files, sent } = setup({ confirm: async () => false });
  broker.handleControl(call("call_4", move));
  const response = await answer(sent, "call_4");
  assert.equal((response.content as { status: string }).status, "declined");
  assert.equal(files.runs, 0);
});

test("when the runtime gives up waiting, the question is withdrawn and nothing runs", async () => {
  const { broker, files, sent } = setup({
    confirm: (_r, signal) => new Promise((resolve) => signal.addEventListener("abort", () => resolve(false))),
  });
  broker.handleControl(call("call_5", move));
  await sleep(10);
  broker.handleControl({ type: "tool.timeout", call_id: "call_5" });
  const response = await answer(sent, "call_5");
  assert.equal((response.content as { status: string }).status, "declined");
  assert.match((response.content as { message: string }).message, /did not answer in time/);
  assert.equal(files.runs, 0);
});

test("a tool the runtime did not agree to is not run", async () => {
  const { broker, files, sent } = setup();
  broker.handleControl({ type: "ready", host_tools: { accepted: [] } });
  broker.handleControl(call("call_6", move));
  const response = await answer(sent, "call_6");
  assert.equal((response.content as { status: string }).status, "unsupported");
  assert.equal(files.runs, 0);
});

test("arguments the catalog does not allow are refused before anything is prepared", async () => {
  const { broker, files, sent } = setup();
  broker.handleControl(call("call_7", { action: "delete", path: "report.pdf" }));
  const response = await answer(sent, "call_7");
  assert.equal((response.content as { status: string }).status, "failed");
  assert.match((response.content as { message: string }).message, /files\.action is not one of/);
  assert.equal(files.runs, 0);
});

test("a missing permission is reported, and macOS is asked to show its prompt", async () => {
  const setupResult = setup({ trusted: false });
  setupResult.files.permission = "accessibility";
  setupResult.broker.handleControl(call("call_8", move));
  const response = await answer(setupResult.sent, "call_8");
  const content = response.content as { status: string; permission: string; message: string };
  assert.equal(content.status, "needs_permission");
  assert.equal(content.permission, "accessibility");
  assert.match(content.message, /Privacy & Security → Accessibility/);
  assert.deepEqual(setupResult.prompts, [false, true]);
  assert.equal(setupResult.files.runs, 0);
  const failed = setupResult.events.find((e) => e.type === "action.failed")!;
  assert.equal(failed.category, "accessibility_denied");
});

test("a target that is not there comes back as not_found, with nothing run", async () => {
  const { broker, files, sent } = setup();
  broker.handleControl(call("call_9", { action: "move", path: "report.pdf", to: "Nowhere" }));
  const response = await answer(sent, "call_9");
  assert.deepEqual(response.content, { status: "not_found", message: "No folder called Nowhere." });
  assert.equal(files.runs, 0);
});

test("calls that are not addressed to this desktop are left alone", () => {
  const { broker } = setup();
  assert.equal(broker.handleControl({ type: "tool.call", tool_calls: [{ name: "task_start" }] }), false);
});

test("results are cut to fit what the model can take in, keeping status and message", () => {
  const long = fit({
    status: "done",
    message: "Found many.",
    found: Array.from({ length: 200 }, (_, i) => `~/Downloads/some-long-file-name-${i}.pdf (2 h ago)`),
  });
  assert.ok(JSON.stringify(long).length <= 900);
  assert.equal(long.status, "done");
  assert.equal(long.message, "Found many.");
  assert.equal(long.more, true);
});

test("after a change, the model is told whether it can check it by looking", async () => {
  for (const [look, note] of [
    ["fresh", /taken after this is with you now/],
    ["not_shared", /cannot be checked by looking/],
  ] as const) {
    const { broker, sent } = setup({ look, turn: { turnId: "t", text: "move it into Projects", endedAtMs: Date.now() } });
    broker.handleControl(call(`call_look_${look}`, move));
    const response = await answer(sent, `call_look_${look}`);
    assert.match(String((response.content as { screen: string }).screen), note);
  }
});

test("an action that arrives before the person's words are transcribed waits for them, then runs without asking", async () => {
  let turn: TrustedTurn | null = null;
  const { broker, files, sent, asked } = setup({
    latestTurn: () => turn,
    waitForWords: async () => {
      await sleep(30);
      turn = { turnId: "t-late", text: "move the report into Projects", endedAtMs: Date.now() };
    },
  });
  broker.handleControl(call("call_words", move));
  const response = await answer(sent, "call_words");
  assert.equal((response.content as { status: string }).status, "done");
  assert.equal(asked.length, 0);
  assert.equal(files.runs, 1);
});

test("the person is kept up to date on every call: asked, working, then how it went", async () => {
  // Asked for in their own words: straight to work.
  const direct = setup({ turn: { turnId: "t1", text: "move the report into Projects", endedAtMs: Date.now() } });
  direct.broker.handleControl(call("c1", move));
  await answer(direct.sent, "c1");
  assert.deepEqual(direct.updates, ["working", "done"]);
  // Not asked for: the person is asked first.
  const asked = setup();
  asked.broker.handleControl(call("c2", move));
  await answer(asked.sent, "c2");
  assert.deepEqual(asked.updates, ["waiting", "working", "done"]);
  // A failure says so, and the log says why without saying what.
  const failed = setup({ turn: { turnId: "t1", text: "move it to Nowhere", endedAtMs: Date.now() } });
  failed.broker.handleControl(call("c3", { ...move, to: "Nowhere" }));
  await answer(failed.sent, "c3");
  assert.deepEqual(failed.updates, ["failed"]);
  assert.ok(failed.logs.some((l) => /call c3 files → not_found \[not_found\]/.test(l)));
});

test("the log tells a call that never matched the tool from one that failed while running", async () => {
  const { broker, sent, logs } = setup();
  broker.handleControl(call("c4", { action: "move", path: 42 }));
  await answer(sent, "c4");
  assert.ok(logs.some((l) => /call c4 files → failed \[arguments_rejected\]/.test(l)), logs.join("\n"));
});

test("an allowed action that types or clicks runs through the host's input hook; others do not", async () => {
  const registry = new ToolRegistry({ runtimeUrl: "http://127.0.0.1:1" });
  const ran: string[] = [];
  const tool = (name: string, effect: "input" | "change"): ActionTool => ({
    name,
    platforms: [process.platform],
    async prepare(args) {
      return {
        tool: name,
        action: String(args.action),
        effect,
        summary: `${name} ${String(args.action)}`,
        scope: [{ value: "x", source: "named" }],
        needs: [],
        run: async () => {
          ran.push(`${name}:run`);
          return { verified: "none", message: "Done." };
        },
      };
    },
  });
  registry.registerAction(tool("input", "input"));
  registry.registerAction(tool("files", "change"));
  const sent: Array<Record<string, unknown>> = [];
  const broker = new ActionBroker({
    registry,
    send: (c) => sent.push(c),
    event: () => {},
    confirm: async () => true,
    accessibility: () => true,
    latestTurn: () => null,
    log: () => {},
    aroundInput: async (run) => {
      ran.push("hook:before");
      try {
        return await run();
      } finally {
        ran.push("hook:after");
      }
    },
  });
  broker.handleControl({ type: "ready", host_tools: { accepted: ["input", "files"] } });
  const request = (callId: string, name: string) => ({
    type: "tool.call",
    call_id: callId,
    dispatch: "client",
    tool_calls: [{ name, arguments: name === "input" ? { action: "keys", keys: "cmd+v" } : move }],
    tool_response_expected: true,
  });
  broker.handleControl(request("c1", "input"));
  const response = await answer(sent, "c1");
  assert.deepEqual(ran, ["hook:before", "input:run", "hook:after"], JSON.stringify(response));
  ran.length = 0;
  broker.handleControl(request("c2", "files"));
  await answer(sent, "c2");
  assert.deepEqual(ran, ["files:run"], "a file move does not touch the front app");
});


test("foreground desktop actions never overlap", async () => {
  const registry = new ToolRegistry({ runtimeUrl: "http://127.0.0.1:1" });
  let releaseFirst: () => void = () => {};
  const firstGate = new Promise<void>((resolve) => { releaseFirst = resolve; });
  const order: string[] = [];
  let runs = 0;
  const input: ActionTool = {
    name: "input",
    platforms: [process.platform],
    async prepare(args) {
      const id = String(args.keys);
      return {
        tool: "input",
        action: "keys",
        effect: "input",
        summary: `Press ${id}`,
        scope: [{ value: id, source: "named" }],
        run: async () => {
          runs += 1;
          const n = runs;
          order.push(`start:${n}`);
          if (n === 1) await firstGate;
          order.push(`end:${n}`);
          return { verified: "none", message: "Done." };
        },
      };
    },
  };
  registry.registerAction(input);
  const sent: Array<Record<string, unknown>> = [];
  const broker = new ActionBroker({
    registry,
    send: (control) => sent.push(control),
    event: () => {},
    confirm: async () => true,
    accessibility: () => true,
    latestTurn: () => null,
    log: () => {},
  });
  broker.handleControl({ type: "ready", host_tools: { accepted: ["input"] } });
  const request = (id: string, keys: string) => ({
    type: "tool.call",
    call_id: id,
    dispatch: "client",
    tool_calls: [{ name: "input", arguments: { action: "keys", keys } }],
    tool_response_expected: true,
  });

  broker.handleControl(request("lease-1", "cmd+c"));
  broker.handleControl(request("lease-2", "cmd+v"));
  await sleep(20);
  assert.deepEqual(order, ["start:1"], "the second desktop mutation must wait");
  releaseFirst();
  await Promise.all([answer(sent, "lease-1"), answer(sent, "lease-2")]);
  assert.deepEqual(order, ["start:1", "end:1", "start:2", "end:2"]);
});
