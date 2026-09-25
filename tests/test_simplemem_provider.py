"""Tests for the Omni-SimpleMem provider against a fake sidecar.

Covers the contract the service and the realtime recall path depend on:
approval-gated writes, repo-scoped namespaces, structured provenance,
typed-memory mapping, and fail-loud behavior when the configured memory
service is unreachable.
"""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from gnsis.memory import MemoryRecord, SimpleMemProvider
from gnsis.memory.simplemem import (
    SimpleMemUnavailable,
    memory_type_for_kind,
)

TOKEN = "x" * 40


class _FakeSidecar(BaseHTTPRequestHandler):
    requests = []
    store = {}  # namespace -> [records]

    def log_message(self, *args):
        pass

    def _auth_ok(self):
        return self.headers.get("X-GNSIS-SimpleMem-Token") == TOKEN

    def _reply(self, payload, code=200):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_PUT(self):
        _FakeSidecar.requests.append((self.command, self.path))
        if not self._auth_ok():
            return self._reply({"detail": "unauthorized"}, 401)
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        ns = self.path.split("/")[2]
        record = {
            "mauId": f"mau-{len(_FakeSidecar.store.get(ns, []))}",
            "type": body.get("type"),
            "summary": body.get("text", "")[:100],
            "text": body.get("text", ""),
            "provenance": body.get("provenance") or {},
            "createdAtUnix": 1,
        }
        _FakeSidecar.store.setdefault(ns, []).append(record)
        self._reply({"mauId": record["mauId"], "durableArchive": False})

    def do_POST(self):
        _FakeSidecar.requests.append((self.command, self.path))
        if not self._auth_ok():
            return self._reply({"detail": "unauthorized"}, 401)
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        ns = self.path.split("/")[2]
        query = body.get("query", "").lower()
        types = body.get("types")
        hits = [
            r
            for r in _FakeSidecar.store.get(ns, [])
            if query.split()[0] in r["summary"].lower()
            and (not types or r["type"] in types)
        ]
        self._reply({"items": hits, "results": hits, "totalCandidates": len(hits)})

    def do_GET(self):
        if self.path == "/health":
            return self._reply({"ok": True})
        if self.path == "/ready":
            if not self._auth_ok():
                return self._reply({"detail": "unauthorized"}, 401)
            return self._reply({"ok": True})
        _FakeSidecar.requests.append((self.command, self.path))
        if not self._auth_ok():
            return self._reply({"detail": "unauthorized"}, 401)
        ns = self.path.split("/")[2]
        items = list(reversed(_FakeSidecar.store.get(ns, [])))
        self._reply({"items": items, "results": items})


class SidecarFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeSidecar)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        _FakeSidecar.requests.clear()
        _FakeSidecar.store.clear()
        self.provider = SimpleMemProvider(self.url, token=TOKEN)


class ConfigTests(unittest.TestCase):
    def test_requires_url(self):
        with self.assertRaises(ValueError):
            SimpleMemProvider("not-a-url", token=TOKEN)

    def test_requires_token(self):
        with self.assertRaises(ValueError):
            SimpleMemProvider("http://localhost:1", token="")

    def test_short_token_refused(self):
        with self.assertRaises(ValueError):
            SimpleMemProvider("http://localhost:1", token="short")

    def test_type_mapping(self):
        self.assertEqual(memory_type_for_kind("decision"), "decision")
        self.assertEqual(memory_type_for_kind("convention"), "fact")
        self.assertEqual(memory_type_for_kind("unknown-kind"), "fact")
        self.assertEqual(
            memory_type_for_kind("approved_code_intelligence"),
            "approved_code_intelligence",
        )


class ProviderContractTests(SidecarFixture):
    def test_unapproved_write_is_refused_without_http(self):
        rec = MemoryRecord(repo="o/r", content="x", approved=False)
        self.assertIsNone(self.provider.write(rec))
        self.assertEqual(_FakeSidecar.requests, [])

    def test_write_then_cross_session_recall(self):
        # A "first session" writes an approved memory...
        rec = MemoryRecord(
            repo="o/r",
            content="deploys require the canary gate",
            kind="convention",
            approved=True,
            memory_id="mem-1",
            source_job_id="job-7",
        )
        written = self.provider.write(rec)
        self.assertIsNotNone(written)

        # ...and a later session recalls it through a fresh provider instance.
        later = SimpleMemProvider(self.url, token=TOKEN)
        hits = later.search("o/r", "deploys", limit=5)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].kind, "fact")  # convention maps to fact
        self.assertEqual(hits[0].metadata["mau_id"], "mau-0")
        self.assertEqual(hits[0].memory_id, "mem-1")
        self.assertEqual(hits[0].source_job_id, "job-7")

    def test_namespace_isolation(self):
        self.provider.write(MemoryRecord(repo="o/a", content="alpha", approved=True))
        self.provider.write(MemoryRecord(repo="o/b", content="beta", approved=True))
        hits_a = self.provider.search("o/a", "alpha")
        hits_b = self.provider.search("o/b", "beta")
        self.assertEqual(len(hits_a), 1)
        self.assertEqual(len(hits_b), 1)
        # The repos stored under distinct namespaces.
        namespaces = {path.split("/")[2] for _, path in _FakeSidecar.requests}
        self.assertGreaterEqual(len(namespaces), 2)

    def test_recent_returns_newest_first(self):
        self.provider.write(MemoryRecord(repo="o/r", content="first", approved=True))
        self.provider.write(MemoryRecord(repo="o/r", content="second", approved=True))
        recent = self.provider.recent("o/r", limit=2)
        self.assertEqual([r.content for r in recent], ["second", "first"])

    def test_unauthorized_raises(self):
        bad = SimpleMemProvider(self.url, token="y" * 40)
        with self.assertRaises(SimpleMemUnavailable):
            bad.search("o/r", "x")

    def test_unreachable_raises(self):
        dead = SimpleMemProvider("http://127.0.0.1:1", token=TOKEN, timeout_s=0.5)
        with self.assertRaises(SimpleMemUnavailable):
            dead.recent("o/r")

    def test_write_episode_carries_provenance(self):
        self.provider.write_episode(
            "o/r",
            text="the screen showed a red build badge",
            memory_type="visual_event",
            provenance={
                "source_session_id": "sess-1",
                "source_start_sec": 12.0,
                "source_end_sec": 14.5,
            },
            session_id="sess-1",
        )
        hits = self.provider.search("o/r", "screen", types=["visual_event"])
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].kind, "visual_event")
        self.assertEqual(hits[0].metadata["source_session_id"], "sess-1")

    def test_ready_health(self):
        self.assertTrue(self.provider.ready()["ok"])
        with self.assertRaises(SimpleMemUnavailable):
            SimpleMemProvider(self.url, token="z" * 40).assert_ready()


if __name__ == "__main__":
    unittest.main()
