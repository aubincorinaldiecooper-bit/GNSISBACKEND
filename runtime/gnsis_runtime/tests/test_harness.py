"""Harness daemon/ACP boundary tests: discovery, client, bridge diff."""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.parse
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from gnsis_runtime.harness import (
    HarnessBridge,
    HarnessDaemonClient,
    HarnessDiscovery,
    HarnessUnavailable,
)
from gnsis_runtime.timeline import SessionTimeline


def _write_discovery(tmp_path, **overrides):
    payload = {
        "url": "http://127.0.0.1:4321",
        "protocolVersion": 1,
        "pid": 4242,
        "instanceNonce": "nonce-abc",
        "token": "tok-123",
    }
    payload.update(overrides)
    path = tmp_path / "discovery.json"
    path.write_text(json.dumps(payload))
    os.chmod(path, 0o600)
    return path


def _fake_urlopen(responses):
    """Return a urlopen stub serving {path: dict} JSON bodies."""

    class Res:
        def __init__(self, body):
            self._body = json.dumps(body).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self._body

    def open_(req, timeout=None):
        path = urllib.parse.urlparse(req.full_url).path
        if path not in responses:
            raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)
        body = responses[path]
        if callable(body):
            body = body(req)
        return Res(body)

    return open_


def _client(tmp_path):
    discovery = HarnessDiscovery.load(_write_discovery(tmp_path))
    return HarnessDaemonClient(discovery)


def test_discovery_load_and_validation(tmp_path):
    d = HarnessDiscovery.load(_write_discovery(tmp_path))
    assert d.url == "http://127.0.0.1:4321"
    assert d.instance_nonce == "nonce-abc"
    assert d.token == "tok-123"

    bad = _write_discovery(tmp_path, pid=-1)
    with pytest.raises(HarnessUnavailable):
        HarnessDiscovery.load(bad)

    loose = _write_discovery(tmp_path)
    os.chmod(loose, 0o644)
    with pytest.raises(HarnessUnavailable):
        HarnessDiscovery.load(loose)


def test_client_calls_control_surface(tmp_path):
    client = _client(tmp_path)
    seen = []

    def subagents(req):
        seen.append(json.loads(req.data))
        return {"type": "outcome", "outcome": "stopping", "taskId": "t1"}

    responses = {
        "/healthz": None,
        "/live/instance": {"pid": 1, "instanceNonce": "n", "protocolVersion": 1},
        "/live/subagents": subagents,
    }
    with patch("urllib.request.urlopen", _fake_urlopen(responses)):
        assert client.healthz()
        assert client.instance()["pid"] == 1
        client.stop_task("t1")
        assert seen[-1] == {"action": "stop", "taskId": "t1"}
        client.decide_permission("h1", "allow", scope="once")
        assert seen[-1] == {
            "action": "permission",
            "requestHandle": "h1",
            "decision": "allow",
            "scope": "once",
        }
    with pytest.raises(ValueError):
        asyncio.run(_bad_decision(client))


async def _bad_decision(client):
    client.decide_permission("h", "maybe")


def test_client_error_raises_unavailable(tmp_path):
    client = _client(tmp_path)
    with patch("urllib.request.urlopen", _fake_urlopen({})):
        with pytest.raises(HarnessUnavailable):
            client.list_subagents()


def _coordinator(tmp_path):
    delivery = MagicMock()
    delivery.enqueue = MagicMock(
        return_value=SimpleNamespace(delivery_id="deliv-1")
    )
    gate = MagicMock()
    coordinator = SimpleNamespace(
        gateway=SimpleNamespace(delivery=delivery),
        gate=gate,
        owner_id="sess-1",
        timeline=SessionTimeline(
            "sess-1", log_path=tmp_path / "tl.jsonl"
        ),
    )
    return coordinator


def test_bridge_diff_emits_and_enqueues_terminal(tmp_path):
    coordinator = _coordinator(tmp_path)
    client = MagicMock()
    bridge = HarnessBridge(coordinator, client, poll_sec=60)

    snapshot = {
        "revision": 2,
        "counts": {"running": 0, "completed": 1},
        "tasks": [
            {
                "id": "harness:job1",
                "kind": "harness",
                "backend": "codex",
                "title": "fix the bug",
                "status": "completed",
                "output": "Done: fixed.",
                "permissions": [
                    {"requestHandle": "perm-1", "backend": "codex", "title": "run rm?"}
                ],
            }
        ],
    }
    bridge._diff({"revision": 1, "counts": {}, "tasks": [
        {"id": "harness:job1", "kind": "harness", "status": "running",
         "title": "fix the bug"}
    ]})
    bridge._diff(snapshot)

    # Terminal status -> one Gateway delivery enqueue, status carried through.
    coordinator.gateway.delivery.enqueue.assert_called_once()
    kwargs = coordinator.gateway.delivery.enqueue.call_args.kwargs
    assert kwargs["task_id"] == "harness:job1"
    assert kwargs["topic"] == "final"
    assert kwargs["status"] == "completed"
    assert kwargs["timing"] == "safe_pause"
    assert kwargs["speech_hint"] == "Done: fixed."
    coordinator.gate.register.assert_called_once()

    # Timeline got task.delegated, task.progress, permission.requested,
    # result_ready — and re-diffing the same snapshot enqueues nothing new.
    bridge._diff(snapshot)
    coordinator.gateway.delivery.enqueue.assert_called_once()
    events = [json.loads(line) for line in (tmp_path / "tl.jsonl").read_text().splitlines()]
    kinds = [e["kind"] for e in events]
    assert "task.delegated" in kinds
    assert "task.progress" in kinds
    assert "permission.requested" in kinds
    assert "result_ready" in kinds


def test_bridge_dedupes_permissions_and_regression(tmp_path):
    coordinator = _coordinator(tmp_path)
    bridge = HarnessBridge(coordinator, MagicMock(), poll_sec=60)
    task = {
        "id": "harness:j2",
        "status": "running",
        "permissions": [{"requestHandle": "p9", "title": "x"}],
    }
    bridge._diff({"tasks": [task]})
    bridge._diff({"tasks": [task]})
    events = [
        json.loads(line)["kind"]
        for line in (tmp_path / "tl.jsonl").read_text().splitlines()
    ]
    assert events.count("permission.requested") == 1


@pytest.mark.asyncio
async def test_bridge_poll_loop_tolerates_failure(tmp_path):
    coordinator = _coordinator(tmp_path)
    client = MagicMock()
    client.list_subagents.side_effect = HarnessUnavailable("down")
    bridge = HarnessBridge(coordinator, client, poll_sec=0.01)
    bridge.start()
    await asyncio.sleep(0.05)
    await bridge.stop()
    events = [
        json.loads(line)["kind"]
        for line in (tmp_path / "tl.jsonl").read_text().splitlines()
    ]
    assert "harness.poll_failed" in events
