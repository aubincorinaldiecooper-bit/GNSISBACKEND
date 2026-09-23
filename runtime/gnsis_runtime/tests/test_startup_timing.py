"""Regression tests for the gnsis_startup / gnsis_session_timing instrumentation.

These cover the timing helper itself and the truthful /health additions —
no model is loaded; marks are exercised directly.
"""

from __future__ import annotations

import logging
import re
import time

import pytest

from mcpmft.infer import startup_timing
from mcpmft.infer.startup_timing import _Stage


@pytest.fixture(autouse=True)
def _clean_records(monkeypatch):
    monkeypatch.setattr(startup_timing, "_STAGES", [])
    monkeypatch.setattr(startup_timing, "_FIRST_EVENTS", {})
    yield


def test_attempt_id_is_stable_eight_hex():
    assert re.fullmatch(r"[0-9a-f]{8}", startup_timing.attempt_id())
    assert startup_timing.attempt_id() == startup_timing.attempt_id()


def test_stage_records_valid_start_end_pair():
    with startup_timing.stage("thinker_model_load", gpu=0):
        time.sleep(0.001)
    record = startup_timing._STAGES[0]
    assert record.end_ms is not None
    assert record.end_ms >= record.start_ms
    assert startup_timing.stage_duration_ms("thinker_model_load") >= 0
    assert record.gpu == 0


def test_mark_end_without_start_does_not_crash(caplog):
    with caplog.at_level(logging.INFO, logger="mcpmft.infer.startup_timing"):
        startup_timing.mark("never_started", "end")
    assert any("event=end" in r.getMessage() for r in caplog.records)


def test_log_line_format_has_required_fields(caplog):
    with caplog.at_level(logging.INFO, logger="mcpmft.infer.startup_timing"):
        startup_timing.mark("container_process_start", "start")
    message = caplog.records[0].getMessage()
    assert message.startswith("gnsis_startup startup_attempt=")
    assert "stage=container_process_start" in message
    assert "event=start" in message
    assert "elapsed_ms=" in message


def test_marks_never_leak_payload_values(caplog):
    """Fields are machine tokens only: no field value may be a long blob."""

    with caplog.at_level(logging.INFO, logger="mcpmft.infer.startup_timing"):
        startup_timing.mark("x", "end", tokens=1697, ok=True)
    message = caplog.records[0].getMessage()
    for kv in message.split():
        if "=" in kv:
            assert len(kv.rsplit("=", 1)[1]) < 128


def test_covered_ms_uses_interval_union_not_naive_sum():
    # Two 100ms stages running fully in parallel cover 100ms, not 200.
    now = 1000
    startup_timing._STAGES.extend(
        [
            _Stage("a", None, now, now + 100),
            _Stage("b", None, now, now + 100),
        ]
    )
    covered, serial, overlap = startup_timing.covered_ms()
    assert covered == 100
    assert serial == 200
    assert overlap == 100


def test_overlap_does_not_inflate_unexplained_time():
    # Two fully-parallel 100ms stages inside a 300ms boot: the naive serial
    # sum (200) would leave 100ms "unexplained"; the union leaves 200.
    startup_timing._STAGES.extend(
        [_Stage("a", None, 50, 150), _Stage("b", None, 50, 150)]
    )
    covered, serial, overlap = startup_timing.covered_ms()
    total = 300
    assert total - covered == 200
    assert total - serial == 100  # the wrong answer, for contrast
    assert overlap == 100


def test_open_stage_excluded_from_covered():
    startup_timing._STAGES.append(
        _Stage("open", None, startup_timing.process_ms())
    )
    covered, serial, overlap = startup_timing.covered_ms()
    assert covered == 0 and serial == 0 and overlap == 0


def test_health_fields_only_truthful_keys():
    fields = startup_timing.health_fields()
    assert set(fields) == {
        "startup_attempt",
        "startup_class",
        "startup_total_seconds",
        "startup_stage_seconds",
    }
    assert fields["startup_class"] == "cold"
    assert re.fullmatch(r"[0-9a-f]{8}", fields["startup_attempt"])


def test_health_stage_seconds_map_known_stages():
    with startup_timing.stage("detached_talker_init", gpu=1):
        pass
    fields = startup_timing.health_fields()
    assert "talker" in fields["startup_stage_seconds"]
    assert fields["startup_stage_seconds"]["talker"] >= 0


def test_no_talker_stages_when_never_marked():
    startup_timing.health_fields()
    with startup_timing.stage("thinker_model_load"):
        pass
    fields = startup_timing.health_fields()
    assert "talker" not in fields["startup_stage_seconds"]
    assert "detached_speech_warmup" not in fields["startup_stage_seconds"]


def test_observe_once_records_first_event_only():
    startup_timing.observe_once("runtime_ready")
    first = startup_timing.first_event_at("runtime_ready")
    startup_timing.observe_once("runtime_ready")
    assert startup_timing.first_event_at("runtime_ready") == first


def test_warm_class_rule_is_deterministic():
    """Warm means the runtime was already ready when the socket arrived —
    the same rule the duplex handler applies."""

    assert startup_timing.first_event_at("runtime_ready") is None
    startup_timing.observe_once("runtime_ready")
    assert startup_timing.first_event_at("runtime_ready") is not None


def test_resource_snapshot_never_raises():
    startup_timing.resource_snapshot("after_prefix_prepare")


def test_stage_duration_ms_accessor():
    with startup_timing.stage("prefix_prepare"):
        pass
    assert startup_timing.stage_duration_ms("prefix_prepare") >= 0
    assert startup_timing.stage_duration_ms("missing") is None


def test_startup_only_stages_stop_recording_after_ready():
    """Prefix sub-stages also run per session on the no-snapshot path. Once
    the runtime is ready they must vanish, or the boot record would grow with
    session count and the health numbers would drift."""

    with startup_timing.stage("prefix_duplex_prefill", startup_only=True):
        pass
    assert startup_timing.stage_duration_ms("prefix_duplex_prefill") is not None
    boot_stages = len(startup_timing._STAGES)

    startup_timing.observe_once("runtime_ready")
    with startup_timing.stage("prefix_duplex_prefill", startup_only=True):
        pass
    assert len(startup_timing._STAGES) == boot_stages


def test_startup_only_stage_still_runs_its_body_after_ready():
    startup_timing.observe_once("runtime_ready")
    ran = []
    with startup_timing.stage("prefix_duplex_prefill", startup_only=True):
        ran.append(True)
    assert ran == [True]


def test_cuda_sync_is_a_no_op_without_cuda():
    startup_timing.cuda_sync()
    with startup_timing.stage("prefix_duplex_prefill", sync=True):
        pass
    assert startup_timing.stage_duration_ms("prefix_duplex_prefill") >= 0


def test_prefix_sub_stages_are_reported_on_health():
    for name in ("prefix_duplex_prefill", "prefix_snapshot_capture"):
        with startup_timing.stage(name):
            pass
    keys = startup_timing.health_fields()["startup_stage_seconds"]
    assert "prefix_duplex_prefill" in keys
    assert "prefix_snapshot_capture" in keys


def test_probe_stages_absent_unless_the_probe_ran():
    with startup_timing.stage("prefix_prepare"):
        pass
    keys = startup_timing.health_fields()["startup_stage_seconds"]
    assert "cuda_context_probe" not in keys
    assert "prefix_prefill_repeat" not in keys
