from __future__ import annotations

from mcpmft.eval.hiplex_trace import (
    evaluate_trace,
    map_gander_action,
)


def _event(seq, kind, ts, **fields):
    return {
        "seq": seq,
        "kind": kind,
        "component": "test",
        "session_id": "s1",
        "recv_ts_ms": ts,
        "source_ts_ms": ts,
        "call_epoch": 1,
        "output_epoch": 1,
        "correlation_id": fields.get("playback_id"),
        "fields": fields,
    }


def test_gander_action_mapping_preserves_explicit_interrupt_and_backchannel():
    assert map_gander_action("listen")["hiplex_group"] == "pad_like"
    assert map_gander_action("speak")["hiplex_group"] == "cont_like"
    assert map_gander_action("backchannel")["timing_role"] == "short_acknowledgement"
    assert map_gander_action("interrupt")["hiplex_group"] == "explicit_yield"
    assert map_gander_action("tool")["hiplex_group"] == "excluded"


def test_interruption_uses_real_playback_terminal_and_explicit_interrupt():
    events = [
        _event(1, "foreground.decision", 900, action="speak", generation_id=1),
        _event(2, "playback.started", 950, playback_id="p1", output_epoch=1),
        _event(3, "user.interruption", 1000, reason="speech"),
        _event(4, "foreground.decision", 1080, action="interrupt", generation_id=2),
        _event(5, "playback.cancelled", 1120, playback_id="p1", output_epoch=1),
        _event(6, "foreground.decision", 1450, action="listen", generation_id=2),
        _event(7, "foreground.decision", 2050, action="speak", generation_id=3),
        _event(8, "playback.started", 2100, playback_id="p2", output_epoch=3),
        _event(9, "playback.completed", 2500, playback_id="p2", output_epoch=3),
    ]
    report = evaluate_trace(
        events,
        [
            {
                "id": "barge-1",
                "type": "interruption",
                "start_ms": 1000,
                "end_ms": 1800,
            }
        ],
        merge_gap_ms=100,
    )
    result = report["annotations"][0]
    assert result["metrics"]["speech_after_grace_ms"] == 0
    assert result["metrics"]["yield_latency_ms"] == 120
    assert result["metrics"]["reentry_latency_ms"] == 300
    assert any(
        target["action"] == "interrupt" and target["polarity"] == "positive"
        for target in result["causal_targets"]
    )
    assert report["training_readiness"]["ready_for_offline_credit_analysis"] is True


def test_pause_intrusion_and_backchannel_are_scored_separately():
    events = [
        _event(1, "foreground.decision", 1000, action="backchannel", generation_id=1),
        _event(2, "playback.started", 1010, playback_id="bc", output_epoch=1),
        _event(3, "playback.completed", 1300, playback_id="bc", output_epoch=1),
        _event(4, "foreground.decision", 3100, action="speak", generation_id=2),
        _event(5, "playback.started", 3120, playback_id="bad", output_epoch=2),
        _event(6, "playback.completed", 3800, playback_id="bad", output_epoch=2),
    ]
    report = evaluate_trace(
        events,
        [
            {
                "id": "bc-1",
                "type": "backchannel",
                "start_ms": 800,
                "end_ms": 1400,
                "opportunity_ms": 1000,
            },
            {
                "id": "pause-1",
                "type": "pause",
                "start_ms": 3000,
                "end_ms": 4000,
            },
        ],
        merge_gap_ms=100,
    )
    bc, pause = report["annotations"]
    assert bc["metrics"]["matched"] is True
    assert any(target["action"] == "backchannel" for target in bc["causal_targets"])
    assert pause["metrics"]["intrusion_ms"] == 680
    assert any(
        target["reason"] == "pause_intrusion" and target["polarity"] == "negative"
        for target in pause["causal_targets"]
    )
