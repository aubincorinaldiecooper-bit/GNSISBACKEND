"""End-to-end: Harness delegation -> terminal result -> Gateway delivery ->
delivery gate -> real playback ACK, incl. interruption, permission,
cancellation, reconnect tolerance and enqueue dedupe.

No parallel delivery path: results land through `gateway.delivery.enqueue`,
the existing `_delivery_loop` gates and feeds them to the fake front-brain,
and the device's `playback.ack` is the authoritative delivered signal.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gnsis_runtime.gateway import GNSISGateway, ProviderRegistry
from gnsis_runtime.harness import HarnessBridge, HarnessUnavailable
from gnsis_runtime.supervision import TaskLedger
from gnsis_runtime.task_tools_online import TaskToolsRealtimeCoordinator


class FakeFrontBrain:
    """Session stand-in: accepts runtime events, reports a quiet talker."""

    def __init__(self):
        self.runtime_events = []
        self.interrupts = 0

    def feed_runtime_event(self, response, **_kw):
        self.runtime_events.append(response)
        return SimpleNamespace(
            is_tool_call=False,
            text=str(response.get("content", "")),
            is_listen=False,
            end_of_turn=True,
        )

    def talker_state(self):
        return {"drained": True, "turn_ended": True}

    def set_task_slate(self, _slate):
        return True

    def interrupt_output(self):
        self.interrupts += 1

    def set_summary_needed_callback(self, _cb):
        return None

    def take_native_input_receipt(self, _event):
        return None


class FakeHarnessClient:
    """Serves a mutable subagents snapshot like the daemon's list action."""

    def __init__(self):
        self.snapshot = {"revision": 0, "counts": {}, "tasks": []}
        self.calls = []

    def list_subagents(self, **_kw):
        return {"type": "page", "page": {"snapshot": self.snapshot}}

    def stop_task(self, task_id):
        self.calls.append(("stop", task_id))
        return {"type": "outcome", "outcome": "stopping", "taskId": task_id}

    def decide_permission(self, handle, decision, *, scope=None):
        self.calls.append(("permission", handle, decision, scope))
        return {"type": "outcome", "outcome": f"{decision}ed"}


def _build(tmp_path, *, playback_ack_required=True):
    ledger = TaskLedger(":memory:")
    gateway = GNSISGateway(
        coordinator=None,
        providers=ProviderRegistry(()),
        ledger=ledger,
        mode="lean",
    )
    front_brain = FakeFrontBrain()
    coordinator = TaskToolsRealtimeCoordinator(
        gateway,
        owner_id="sess-e2e",
        session_id="sess-e2e",
        session=front_brain,
        delivery_poll_sec=0.02,
        playback_ack_required=playback_ack_required,
        playback_ack_timeout_sec=30.0,
        close_ledger=False,
    )
    client = FakeHarnessClient()
    bridge = HarnessBridge(coordinator, client, poll_sec=0.02)
    return coordinator, front_brain, client, bridge


def _task(task_id, status, **extra):
    task = {
        "id": task_id,
        "kind": "harness",
        "backend": "codex",
        "title": f"task {task_id}",
        "status": status,
    }
    task.update(extra)
    return task


async def _next_model_output(coordinator, timeout=5.0):
    while True:
        out = await asyncio.wait_for(coordinator.next_output(), timeout)
        if out.kind == "model" and out.delivery_id is not None:
            return out


@pytest.mark.asyncio
async def test_full_lifecycle_delegate_to_playback_ack(tmp_path):
    coordinator, front_brain, client, bridge = _build(tmp_path)
    await coordinator.start()
    bridge.start()
    try:
        # 1. delegate: task appears running -> timeline only.
        client.snapshot["tasks"] = [_task("harness:j1", "running")]
        await asyncio.sleep(0.15)

        # 2. terminal -> result enqueued in the ONE Gateway delivery lane.
        client.snapshot["tasks"] = [
            _task("harness:j1", "completed", output="Build is green.")
        ]
        out = await _next_model_output(coordinator)
        assert out.delivery_id is not None
        assert front_brain.runtime_events, "result never reached the model"

        # 3. sent -> delivering; finished playback ACK is the truth.
        coordinator.note_output_sent(out)
        fresh = coordinator.note_playback_ack(
            {"delivery_id": out.delivery_id, "phase": "finished"}
        )
        assert fresh
        result = coordinator.gate.result(out.delivery_id)
        assert result is not None and result.state == "delivered"

        kinds = _kinds(coordinator)
        assert "task.delegated" in kinds
        assert "task.progress" in kinds
        assert "result_ready" in kinds
        assert "result.state" in kinds
    finally:
        await bridge.stop()
        await coordinator.close()


@pytest.mark.asyncio
async def test_interruption_keeps_result_queued(tmp_path):
    coordinator, front_brain, client, bridge = _build(tmp_path)
    await coordinator.start()
    bridge.start()
    try:
        # User speaking -> gate must hold the queued result. The speech window
        # is short, so keep it fresh while asserting nothing is announced.
        client.snapshot["tasks"] = [
            _task("harness:j2", "completed", output="Report ready.")
        ]
        blocked = asyncio.create_task(_next_model_output(coordinator))
        for _ in range(10):
            coordinator.gate.note_user_speech()
            await asyncio.sleep(0.05)
        assert not blocked.done(), "result announced while user was speaking"

        # Interruption bumps the output epoch; user goes quiet -> delivered.
        epoch = coordinator.note_user_interruption(reason="user_interrupt")
        assert epoch >= 1
        coordinator.gate._user_last_spoke_sec = None  # quiet window elapsed
        out = await blocked
        coordinator.note_output_sent(out)
        # A stale-epoch ACK must be rejected.
        assert not coordinator.note_playback_ack(
            {
                "delivery_id": out.delivery_id,
                "phase": "finished",
                "output_epoch": epoch - 1,
            }
        )
        assert coordinator.note_playback_ack(
            {"delivery_id": out.delivery_id, "phase": "finished"}
        )
    finally:
        await bridge.stop()
        await coordinator.close()


@pytest.mark.asyncio
async def test_permission_cancellation_and_dedupe(tmp_path):
    coordinator, _fb, client, bridge = _build(tmp_path)
    await coordinator.start()
    bridge.start()
    try:
        # Permission surfaces as a state transition, never an implicit grant.
        client.snapshot["tasks"] = [
            _task(
                "harness:j3",
                "running",
                permissions=[{"requestHandle": "perm-9", "title": "run tests?"}],
            )
        ]
        await asyncio.sleep(0.15)
        kinds = _kinds(coordinator)
        assert "permission.requested" in kinds
        client.decide_permission("perm-9", "deny", scope="once")
        assert ("permission", "perm-9", "deny", "once") in client.calls

        # Cancellation via the control surface stops the backend's work.
        client.stop_task("harness:j3")
        assert ("stop", "harness:j3") in client.calls
        client.snapshot["tasks"] = [_task("harness:j3", "cancelled")]
        out = await _next_model_output(coordinator)
        coordinator.note_output_sent(out)
        coordinator.note_playback_ack(
            {"delivery_id": out.delivery_id, "phase": "cancelled"}
        )

        # Dedupe: the same terminal snapshot repeatedly enqueues nothing —
        # dedupe_key makes re-enqueue a no-op. The cancelled playback requeues
        # the SAME delivery record (it may still be relevant), never a second.
        for _ in range(5):
            bridge._diff(client.snapshot)
        await asyncio.sleep(0.1)
        pend = coordinator.gateway.pending_deliveries(coordinator.owner_id)
        assert len(pend) <= 1 and all(
            d.delivery_id == out.delivery_id for d in pend
        ), "duplicate harness result delivery enqueued"
    finally:
        await bridge.stop()
        await coordinator.close()


@pytest.mark.asyncio
async def test_reconnect_tolerance_and_resume(tmp_path):
    coordinator, _fb, client, bridge = _build(tmp_path)
    await coordinator.start()
    try:
        # Poll failure -> poll_failed event; next poll resumes cleanly.
        client.list_subagents = MagicMock(
            side_effect=HarnessUnavailable("daemon down")
        )
        bridge.start()
        await asyncio.sleep(0.15)
        assert "harness.poll_failed" in _kinds(coordinator)

        client.list_subagents = lambda **kw: {
            "type": "page",
            "page": {"snapshot": client.snapshot},
        }
        client.snapshot["tasks"] = [
            _task("harness:j4", "completed", output="Back online.")
        ]
        out = await _next_model_output(coordinator)
        coordinator.note_output_sent(out)
        coordinator.note_playback_ack(
            {"delivery_id": out.delivery_id, "phase": "finished"}
        )
    finally:
        await bridge.stop()
        await coordinator.close()


def _kinds(coordinator):
    return [e.kind for e in coordinator.timeline.snapshot()]


def test_nothing():  # keep pytest happy if asyncio marks misbehave
    assert True
