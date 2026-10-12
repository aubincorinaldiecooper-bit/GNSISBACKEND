import assert from "node:assert/strict";
import test from "node:test";
import { HttpLocalDecisionProvider, localSpeech } from "./localDecisionHttp.js";

test("the local decision service is loopback-only", () => {
  assert.throws(
    () =>
      new HttpLocalDecisionProvider({
        baseUrl: "https://controller.example",
        tools: ["files"],
      }),
    /loopback/,
  );
});

test("the decision request offers only the host tools", async () => {
  let sent: Record<string, unknown> | undefined;
  const provider = new HttpLocalDecisionProvider({
    baseUrl: "http://127.0.0.1:8766",
    tools: ["files", "open"],
    toolSchemas: [{ name: "files", input_schema: { type: "object" } }],
    token: "local-secret",
    request: async (_input, init) => {
      sent = JSON.parse(String(init?.body)) as Record<string, unknown>;
      assert.equal(
        (init?.headers as Record<string, string>).Authorization,
        "Bearer local-secret",
      );
      return new Response(
        JSON.stringify({
          kind: "tool",
          tool: "files",
          args: { action: "list", path: "Desktop" },
          confidence: 0.87,
        }),
      );
    },
  });

  assert.deepEqual(
    await provider.decide({
      requestId: "request-1",
      text: "List my desktop",
      step: 1,
      history: [],
    }),
    {
      kind: "tool",
      tool: "files",
      args: { action: "list", path: "Desktop" },
      confidence: 0.87,
    },
  );
  assert.deepEqual(sent?.tools, ["files", "open"]);
  assert.deepEqual(sent?.tool_schemas, [
    { name: "files", input_schema: { type: "object" } },
  ]);
});

test("malformed local decisions fail closed", async () => {
  const provider = new HttpLocalDecisionProvider({
    baseUrl: "http://localhost:8766",
    tools: ["files"],
    request: async () =>
      new Response(
        JSON.stringify({
          kind: "tool",
          tool: "files",
          args: {},
        }),
      ),
  });

  await assert.rejects(
    provider.decide({
      requestId: "request-2",
      text: "Do it",
      step: 1,
      history: [],
    }),
    /invalid tool decision/,
  );
});

test("local speech is loopback-only and returns bounded PCM16", async () => {
  await assert.rejects(localSpeech("https://gnsis.studio", "Done."), /loopback/);
  let asked = "";
  const pcm = await localSpeech(
    "http://127.0.0.1:8766",
    "Done.",
    "local-secret",
    (async (url: URL) => {
      asked = String(url);
      return new Response(new Uint8Array([0, 0, 1, 0]), { status: 200 });
    }) as unknown as typeof fetch,
  );
  assert.equal(asked, "http://127.0.0.1:8766/v1/speak");
  assert.equal(pcm.length, 4);
});
