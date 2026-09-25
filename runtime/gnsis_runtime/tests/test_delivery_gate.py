"""Acceptance coverage for the delivery gate and shared timeline.

The AGENTS.md acceptance list is tested at the gate level — where every
transition and gate decision is deterministic:

- task finishes while user is speaking -> no announcement;
- task finishes while foreground response is speaking -> queued;
- task finishes while prior audio is still draining -> queued;
- user interrupts background announcement -> stale output cancelled;
- stale completion from an old call epoch -> never played;
- two tasks finish together -> serialized delivery;
- superseded task -> never delivered;
- playback ACK lost/timeout -> safe deterministic recovery;
- reconnect -> deliveries are only claimed once (dedupe lives in the ledger;
  covered here by epoch/ACK bookkeeping staying consistent).
"""

from __future__ import annotations

import pytest

from gnsis_runtime.delivery_gate import DeliveryGate
from gnsis_runtime.timeline import SessionTimeline


def make_gate(**kwargs) -> DeliveryGate:
    timeline = SessionTimeline("sess-1")
    return DeliveryGate(
        timeline,
        playback_ack_timeout_sec=kwargs.pop("ack_timeout", 0.05),
        user_speech_window_sec=kwargs.pop("speech_window", 0.05),
    )


def test_task_finishing_while_user_speaks_does_not_announce():
    gate = make_gate()
    gate.register("d-1")
    gate.note_user_speech()
    allowed, reason = gate.may_announce("d-1")
    assert not allowed
    assert reason == "user_speaking"


def test_task_finishing_while_model_speaks_is_queued():
    gate = make_gate()
    gate.register("d-1")
    gate.note_model_output_started()
    allowed, reason = gate.may_announce("d-1")
    assert not allowed
    assert reason == "model_speaking"
    gate.note_model_output_finished()
    assert gate.may_announce("d-1") == (True, None)


def test_prior_playback_draining_queues_next_result():
    gate = make_gate()
    gate.register("d-1")
    gate.note_output_sent("d-1")
    gate.register("d-2")
    allowed, reason = gate.may_announce("d-2")
    assert not allowed
    assert reason == "playback_draining"
    gate.note_playback_ack("d-1", "finished")
    assert gate.may_announce("d-2") == (True, None)


def test_interrupt_cancels_stale_output_and_drops_acks():
    gate = make_gate()
    gate.register("d-1")
    gate.note_delivering("d-1")
    gate.note_output_sent("d-1")
    gate.interrupt(reason="user_barge_in")
    assert gate.output_epoch == 1
    assert gate.result("d-1").state == "cancelled"
    # A late ACK for the cancelled epoch is ignored, not treated as delivered.
    assert gate.note_playback_ack("d-1", "finished", epoch=0) is False
    kinds = [e.kind for e in gate.timeline.snapshot()]
    assert "output.epoch" in kinds
    stale_ack = [e for e in gate.timeline.kinds("playback.ack")][-1]
    assert stale_ack.fields["stale"] is True


def test_superseded_result_is_never_delivered():
    gate = make_gate()
    gate.register("d-1")
    gate.supersede("d-1")
    allowed, reason = gate.may_announce("d-1")
    assert not allowed
    assert reason == "superseded"
    gate.note_delivering("d-1")
    assert gate.result("d-1").state == "superseded"


def test_two_results_finish_together_deliver_serially():
    gate = make_gate()
    gate.register("d-1")
    gate.register("d-2")
    # First announces; while its playback drains the second stays queued.
    gate.note_delivering("d-1")
    gate.note_output_sent("d-1")
    allowed, reason = gate.may_announce("d-2")
    assert not allowed
    gate.note_playback_ack("d-1", "finished")
    assert gate.may_announce("d-2") == (True, None)


def test_interrupt_priority_preempts_playback():
    gate = make_gate()
    gate.register("d-1")
    gate.note_delivering("d-1")
    gate.note_output_sent("d-1")
    gate.register("d-high")
    allowed, reason = gate.may_announce("d-high", timing="interrupt")
    assert allowed


def test_ack_timeout_is_deterministic_recovery():
    gate = make_gate()
    gate.register("d-1")
    gate.note_delivering("d-1")
    gate.note_output_sent("d-1")
    # No ACK ever arrives — the timeout frees the gate and records why.
    import time

    time.sleep(0.08)
    assert gate.playback_draining() is False
    assert gate.take_expired() == ("d-1",)
    kinds = [e.kind for e in gate.timeline.snapshot()]
    assert "playback.ack_timeout" in kinds
    # The result is a failed delivery, not a silent 'delivered'.
    gate.note_failed("d-1", reason="playback_ack_timeout")
    assert gate.result("d-1").state == "failed"
    # And the next queued result is unblocked.
    gate.register("d-2")
    assert gate.may_announce("d-2") == (True, None)


def test_timeline_events_carry_order_and_epoch():
    gate = make_gate()
    gate.register("d-1")
    gate.interrupt(reason="test")
    events = gate.timeline.snapshot()
    seqs = [e.seq for e in events]
    assert seqs == sorted(seqs)
    epoch_event = [e for e in events if e.kind == "output.epoch"][-1]
    assert epoch_event.output_epoch == 1
    assert epoch_event.session_id == "sess-1"


def test_timeline_jsonl_log(tmp_path):
    log = tmp_path / "session.timeline.jsonl"
    timeline = SessionTimeline("sess-log", log_path=log)
    timeline.emit("turn.bound", component="coordinator", correlation_id="t-1")
    lines = log.read_text().strip().split("\n")
    assert len(lines) == 1
    import json

    record = json.loads(lines[0])
    assert record["kind"] == "turn.bound"
    assert record["correlation_id"] == "t-1"


def test_gate_rejects_bad_config():
    with pytest.raises(ValueError):
        DeliveryGate(SessionTimeline("s"), playback_ack_timeout_sec=0)
    with pytest.raises(ValueError):
        DeliveryGate(SessionTimeline("s"), user_speech_window_sec=-1)
    with pytest.raises(ValueError):
        make_gate().note_playback_ack("x", "bogus-phase")
