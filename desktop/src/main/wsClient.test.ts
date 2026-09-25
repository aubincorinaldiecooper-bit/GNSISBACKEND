import assert from "node:assert/strict";
import { test } from "node:test";
import { once } from "node:events";
import { WebSocketServer, WebSocket } from "ws";
import { ScreenClient } from "./wsClient.js";

/**
 * ScreenClient lifecycle regression tests — a real ws server proves there is
 * never a socket before a token exists, never two sockets on token swap, and
 * that reconnect exhaustion cannot strand the client.
 */

interface SeenConnection {
  token: string | null;
  session_id: string | null;
}

async function withServer(
  fn: (server: WebSocketServer, port: number, seen: SeenConnection[]) => Promise<void>,
): Promise<void> {
  const seen: SeenConnection[] = [];
  const wss = new WebSocketServer({ port: 0 });
  await once(wss, "listening");
  const port = (wss.address() as { port: number }).port;
  wss.on("connection", (ws, req) => {
    const url = new URL(req.url ?? "", "http://x");
    seen.push({
      token: url.searchParams.get("token"),
      session_id: url.searchParams.get("session_id"),
    });
    ws.on("error", () => {});
  });
  try {
    await fn(wss, port, seen);
  } finally {
    for (const client of wss.clients) {
      try {
        client.terminate();
      } catch {
        /* already closed */
      }
    }
    wss.close();
  }
}

const openConnections = (wss: WebSocketServer) =>
  [...wss.clients].filter((c) => c.readyState === WebSocket.OPEN).length;

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

test("no screen socket attempt happens before a token exists", async () => {
  await withServer(async (wss, port, seen) => {
    const client = new ScreenClient({ url: `http://127.0.0.1:${port}`, sessionId: "s1" });
    client.on("error", () => {});
    client.connect();
    await sleep(150);
    assert.equal(seen.length, 0);
    assert.equal(openConnections(wss), 0);
    client.close();
  });
});

test("receiving the initial token starts the authenticated transport", async () => {
  await withServer(async (wss, port, seen) => {
    const client = new ScreenClient({ url: `http://127.0.0.1:${port}`, sessionId: "s1" });
    client.on("error", () => {});
    client.connect();
    await sleep(50);
    client.applyChannel({ token: "tok-1" });
    await sleep(150);
    assert.equal(seen.length, 1);
    assert.equal(seen[0].token, "tok-1");
    assert.equal(seen[0].session_id, "s1");
    assert.equal(openConnections(wss), 1);
    client.close();
  });
});

test("a replacement token never leaves two sockets alive", async () => {
  await withServer(async (wss, port, seen) => {
    const client = new ScreenClient({
      url: `http://127.0.0.1:${port}`,
      sessionId: "s1",
      reconnectDelaysMs: [10],
    });
    client.on("error", () => {});
    client.connect();
    client.applyChannel({ token: "tok-1" });
    await sleep(150);
    assert.equal(seen.length, 1);

    client.applyChannel({ token: "tok-2" });
    await sleep(200);
    assert.equal(seen.length, 2);
    assert.equal(seen[1].token, "tok-2");
    // At no point may two sockets coexist.
    assert.equal(openConnections(wss), 1);
    client.close();
  });
});

test("exhausted pre-auth reconnect state cannot strand the client", async () => {
  // Exhaustion only happens against an unreachable endpoint — a successful
  // open correctly resets the budget. Take a port, then drop the server.
  const probe = new WebSocketServer({ port: 0 });
  await once(probe, "listening");
  const port = (probe.address() as { port: number }).port;
  await new Promise<void>((r) => probe.close(() => r()));

  const client = new ScreenClient({
    url: `http://127.0.0.1:${port}`,
    sessionId: "s1",
    reconnectBudgetMs: 30,
    reconnectDelaysMs: [1],
  });
  client.on("error", () => {});
  const exhausted = new Promise<void>((r) => client.once("reconnect_exhausted", r));
  client.connect();
  client.applyChannel({ token: "tok-1" });
  await exhausted; // budget spent against the dead endpoint

  // A fresh token must still recover once the daemon is reachable again.
  const seen: SeenConnection[] = [];
  const wss = new WebSocketServer({ port });
  await once(wss, "listening");
  wss.on("connection", (ws, req) => {
    const url = new URL(req.url ?? "", "http://x");
    seen.push({ token: url.searchParams.get("token"), session_id: url.searchParams.get("session_id") });
    ws.on("error", () => {});
  });
  client.applyChannel({ token: "tok-2" });
  await sleep(200);
  assert.equal(seen.length, 1);
  assert.equal(seen[0].token, "tok-2");
  client.close();
  for (const ws of wss.clients) ws.terminate();
  wss.close();
});
