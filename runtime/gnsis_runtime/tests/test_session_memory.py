"""Server-owned session recall: config validation and episode shaping."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from gnsis_runtime.session_memory import SessionMemoryRecall


class _Sidecar(BaseHTTPRequestHandler):
    items = []
    seen = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        _Sidecar.seen.append(json.loads(self.rfile.read(length) or b"{}"))
        body = json.dumps({"items": _Sidecar.items}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def recall():
    _Sidecar.items = []
    _Sidecar.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Sidecar)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield SessionMemoryRecall(
        f"http://127.0.0.1:{server.server_port}", namespace="session:test"
    )
    server.shutdown()


def test_url_validation():
    with pytest.raises(ValueError):
        SessionMemoryRecall("not-a-url", namespace="x")
    with pytest.raises(ValueError):
        SessionMemoryRecall("http://u:p@h", namespace="x")
    with pytest.raises(ValueError):
        SessionMemoryRecall("http://h", namespace="")
    with pytest.raises(ValueError):
        SessionMemoryRecall("http://h", namespace="x", top_k=0)
    with pytest.raises(ValueError):
        SessionMemoryRecall("http://h", namespace="x", timeout_s=0)


def test_episodes_for_turn_shapes_provenance(recall):
    _Sidecar.items = [
        {
            "mauId": "mau-9",
            "type": "visual_event",
            "summary": "the screen showed a red build badge",
            "provenance": {
                "source_session_id": "sess-1",
                "source_start_sec": 12.0,
                "source_end_sec": 14.5,
            },
        },
        {"mauId": "", "type": "fact", "summary": "no identity — skipped"},
        {"mauId": "mau-2", "type": "decision", "summary": "chose Postgres audit"},
    ]
    episodes = recall.episodes_for_turn("what happened to the build?")
    assert [e["key"] for e in episodes] == ["recall:mau-9", "recall:mau-2"]
    first = episodes[0]["event_summary"]
    assert first.startswith("[visual_event][session sess-1][12s-14s]")
    assert "red build badge" in first
    assert episodes[0]["end_sec"] > episodes[0]["start_sec"]
    # The turn text became the recall query.
    assert _Sidecar.seen[-1]["query"] == "what happened to the build?"


def test_episodes_for_turn_empty_text(recall):
    assert recall.episodes_for_turn("   ") == []
    assert _Sidecar.seen == []


def test_recall_failure_is_empty_not_fatal(recall):
    # Point the client at a dead port; the live turn must survive.
    recall.base_url = "http://127.0.0.1:1"
    recall.timeout_s = 0.3
    assert recall.episodes_for_turn("hello") == []


def test_repeated_recall_reuses_identity(recall):
    _Sidecar.items = [{"mauId": "mau-1", "type": "fact", "summary": "same"}]
    keys1 = {e["key"] for e in recall.episodes_for_turn("a")}
    keys2 = {e["key"] for e in recall.episodes_for_turn("b")}
    assert keys1 == keys2  # dedupe key is stable across turns
