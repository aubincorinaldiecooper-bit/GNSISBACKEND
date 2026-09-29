import json
from pathlib import Path

import pytest
from PIL import Image

from gnsis_runtime.screen import LatestScreenFrameBuffer, ScreenFrame
from gnsis_runtime.visual.evaluation import evaluate, geometric_score, load_records, main
from gnsis_runtime.visual.real_runs import (
    SCHEMA_VERSION,
    Box,
    BrowserExecutionEvidence,
    Candidate,
    Execution,
    Point,
    RealRunConsent,
    RealRunCoordinator,
    RealRunRecord,
    RealRunRecorder,
    collect_post_action_frames,
    wait_for_post_action_frames,
)
from gnsis_runtime.visual.verification import ExpectedState, SemanticVisualVerifier, VerificationResult


def _image(shade: int = 255) -> Image.Image:
    return Image.new("RGB", (160, 100), (shade, shade, shade))


def _consume(buffer: LatestScreenFrameBuffer, frame_id: str, at: int, shade: int = 255) -> None:
    buffer.publish(ScreenFrame(frame_id, _image(shade), captured_at_ms=at))
    buffer.consume_for_unit()


class Judge:
    name = "fake"

    def __init__(self, verdict):
        self.verdict = verdict
        self.requests = []

    def judge(self, request):
        self.requests.append(request)
        return self.verdict


def test_browser_execution_evidence_parses_bridge_payload():
    evidence = BrowserExecutionEvidence.from_payload(
        {
            "context": "browser",
            "action": "click",
            "source_tab_id": 7,
            "executed_tab_id": 7,
            "started_at_ms": 100,
            "completed_at_ms": 140,
            "latency_ms": 40,
            "source_viewport": {"width": 1280, "height": 800},
            "raw_target": {"x": 640, "y": 400},
            "resolve_target": True,
            "resolution_method": "nearby",
        }
    )
    assert evidence.source_tab_id == 7
    assert evidence.executed_tab_id == 7
    assert evidence.latency_ms == 40
    assert evidence.resolve_target is True


def test_local_collection_can_be_disabled(tmp_path: Path):
    recorder = RealRunRecorder(tmp_path, consent=RealRunConsent(local_collection=False, shared_training=False))
    record = RealRunCoordinator(recorder).finalize(
        run_id="r1", case_id="c1", frame_id="f-1", goal="click continue", action="click", execution={}, post_frame_ids=("f-2",)
    )
    assert record.verified_success is None
    assert record.verification_status == "ambiguous"
    assert not recorder.jsonl_path.exists()


def test_shared_export_requires_explicit_consent(tmp_path: Path):
    recorder = RealRunRecorder(tmp_path, consent=RealRunConsent(local_collection=True, shared_training=False))
    RealRunCoordinator(recorder).finalize(
        run_id="r1", case_id="c1", frame_id="f-1", goal="click continue", action="click", execution={}, post_frame_ids=("f-2",)
    )
    with pytest.raises(PermissionError):
        recorder.export_for_shared_training(tmp_path / "shared.jsonl")
    allowed = RealRunRecorder(tmp_path, consent=RealRunConsent(local_collection=True, shared_training=True))
    assert allowed.export_for_shared_training(tmp_path / "shared.jsonl").read_text() == recorder.jsonl_path.read_text()


def test_defaults_are_local_collection_on_and_shared_training_off(monkeypatch):
    monkeypatch.delenv("GNSIS_REAL_RUN_COLLECTION", raising=False)
    monkeypatch.delenv("GNSIS_SHARED_TRAINING_CONSENT", raising=False)
    assert RealRunConsent.from_env() == RealRunConsent(local_collection=True, shared_training=False)


def test_no_post_frame_is_not_falsely_marked_failed(tmp_path: Path):
    record = RealRunCoordinator(RealRunRecorder(tmp_path)).finalize(
        run_id="r1", case_id="c1", frame_id="f-1", goal="click continue", action="click", execution={}, post_frame_ids=()
    )
    assert record.verified_success is None
    assert record.verification_status == "ambiguous"
    assert record.verification_reason == "No frame from after the action was available."


def test_verification_reads_the_actual_frames_from_shared_history(tmp_path: Path):
    buffer = LatestScreenFrameBuffer(max_history_frames=8)
    _consume(buffer, "f-1", 100)
    _consume(buffer, "f-2", 200)  # after the decision but before the action started
    _consume(buffer, "f-3", 300)
    _consume(buffer, "f-4", 400)
    judge = Judge({"status": "success", "reason": "Prices read CAD", "confidence": 0.9})
    coordinator = RealRunCoordinator(RealRunRecorder(tmp_path), verifier=SemanticVisualVerifier([judge]))
    expected = ExpectedState("Prices are shown in CAD", visible_text=("CAD",))
    result, window = coordinator.verify(
        buffer,
        frame_id="f-1",
        goal="Change the currency to CAD",
        action="click",
        expected_state=expected,
        execution={"actuator_success": True},
        acted_at_ms=250,
        window={"timeout_ms": 0},
    )
    assert result.status == "success"
    assert window.frame_ids == ("f-3", "f-4")
    seen = judge.requests[0]
    assert seen.before.frame_id == "f-1"
    assert [frame.frame_id for frame in seen.after] == ["f-3", "f-4"]
    assert result.evidence["settled"] is True

    record = coordinator.finalize(
        run_id="r1",
        case_id="c1",
        frame_id="f-1",
        goal="Change the currency to CAD",
        action="click",
        verification=result,
        post_frame_ids=window.frame_ids,
        expected_state=expected,
        execution={"executed_variant": "raw", "actuator_success": True, "latency_ms": 31},
    )
    assert record.verified_success is True
    assert record.expected_state == expected
    lines = (tmp_path / "real-runs.jsonl").read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["verification_status"] == "success"


def test_a_frame_that_left_history_or_a_moving_screen_is_ambiguous(tmp_path: Path):
    buffer = LatestScreenFrameBuffer(max_history_frames=8)
    coordinator = RealRunCoordinator(
        RealRunRecorder(tmp_path),
        verifier=SemanticVisualVerifier([Judge({"status": "success", "reason": "x", "confidence": 1.0})]),
    )
    gone, _ = coordinator.verify(
        buffer, frame_id="f-0", goal="g", action="click", expected_state=ExpectedState("x"), execution={}, window={"timeout_ms": 0}
    )
    assert gone.status == "ambiguous" and "no longer in the screen history" in gone.reason

    _consume(buffer, "f-1", 100)
    _consume(buffer, "f-2", 200, shade=0)
    _consume(buffer, "f-3", 300, shade=255)
    moving, window = coordinator.verify(
        buffer, frame_id="f-1", goal="g", action="click", expected_state=ExpectedState("x"), execution={}, window={"timeout_ms": 0}
    )
    assert moving.status == "ambiguous"
    assert window.settled is False and window.timed_out is True


def test_post_action_window_discards_stale_frames_and_waits_for_settle():
    buffer = LatestScreenFrameBuffer(max_history_frames=8)
    _consume(buffer, "f-1", 100)
    _consume(buffer, "f-2", 150)
    window = collect_post_action_frames(buffer, before_frame_id="f-1", acted_at_ms=200, timeout_ms=0)
    assert window.frames == () and window.timed_out
    _consume(buffer, "f-3", 300)
    _consume(buffer, "f-4", 400)
    window = collect_post_action_frames(buffer, before_frame_id="f-1", acted_at_ms=200, timeout_ms=0)
    assert window.frame_ids == ("f-3", "f-4") and window.settled
    with pytest.raises(ValueError):
        collect_post_action_frames(buffer, before_frame_id="f-1", min_frames=0)


def test_waits_for_frames_newer_than_action_source():
    buffer = LatestScreenFrameBuffer(max_history_frames=8)
    buffer.publish(ScreenFrame("f-1", object(), captured_at_ms=100))
    buffer.consume_for_unit()
    buffer.publish(ScreenFrame("f-2", object(), captured_at_ms=200))
    buffer.consume_for_unit()
    buffer.publish(ScreenFrame("f-3", object(), captured_at_ms=300))
    buffer.consume_for_unit()
    assert wait_for_post_action_frames(buffer, before_frame_id="f-1", timeout_ms=0, min_frames=1, max_frames=2) == ("f-2", "f-3")


# ------------------------------------------------------ one record, one reader


def _record(**overrides) -> RealRunRecord:
    values = dict(
        run_id="run-1",
        case_id="case-1",
        captured_at_ms=1000,
        context="browser",
        frame_id="frame-7",
        goal="Click Continue",
        action="click",
        viewport=(1280, 800),
        candidates={
            "raw": Candidate("resolved", Point(90, 90)),
            "raw+r24": Candidate("resolved", Point(110, 110), "nearby"),
            "ocr": Candidate("resolved", Point(115, 115)),
            "ocr+r24": Candidate("resolved", Point(120, 120), "exact-hit"),
        },
        target_box=Box(100, 100, 50, 30),
        execution=Execution(executed_variant="raw+r24", actuator_success=True, latency_ms=18.2),
        verification_status="success",
        verification_reason="Continue page shown",
        verification_confidence=0.9,
    )
    values.update(overrides)
    return RealRunRecord(**values)


def test_record_round_trips_and_verified_success_follows_verification():
    record = _record()
    data = record.to_json()
    assert data["schema_version"] == SCHEMA_VERSION == 2
    assert data["verified_success"] is True
    assert RealRunRecord.from_json(data) == record
    tampered = dict(data, verified_success=False)
    with pytest.raises(ValueError):
        RealRunRecord.from_json(tampered)
    with pytest.raises(ValueError):
        RealRunRecord.from_json(dict(data, schema_version=1))
    with pytest.raises(ValueError):
        _record(candidates={"raw": Candidate("resolved", Point(1280, 20))})
    with pytest.raises(ValueError):
        _record(recovery_success=True)
    with pytest.raises(ValueError):
        _record(verification_status="partial")


def test_scores_recorded_candidates_against_executor_geometry():
    record = _record()
    assert geometric_score(record, "raw") is False
    assert geometric_score(record, "raw+r24") is True
    assert geometric_score(record, "ocr") is True
    assert geometric_score(record, "ocr+r24") is True
    assert geometric_score(_record(target_box=None), "raw+r24") is None
    unprobed = _record(candidates={"raw": Candidate("resolved", Point(110, 110))})
    assert geometric_score(unprobed, "ocr") is None
    abstained = _record(candidates={"raw+r24": Candidate("abstained")})
    assert geometric_score(abstained, "raw+r24") is False


def test_evaluator_reads_the_recorders_file_as_written(tmp_path: Path):
    recorder = RealRunRecorder(tmp_path)
    coordinator = RealRunCoordinator(recorder)
    coordinator.finalize(
        run_id="run-1",
        case_id="c-1",
        frame_id="f-1",
        goal="Click Continue",
        action="click",
        viewport=(1280, 800),
        candidates={"raw": Candidate("resolved", Point(120, 110))},
        target_box=Box(100, 100, 50, 30),
        execution={"executed_variant": "raw", "actuator_success": True, "latency_ms": 20},
        verification=VerificationResult("success", "Continue page shown", 0.9),
        post_frame_ids=("f-2",),
    )
    coordinator.finalize(
        run_id="run-1",
        case_id="c-2",
        frame_id="f-3",
        goal="Click Continue",
        action="click",
        viewport=(1280, 800),
        candidates={"raw": Candidate("resolved", Point(10, 10))},
        target_box=Box(100, 100, 50, 30),
        execution={"executed_variant": "raw", "actuator_success": True, "latency_ms": 40},
        verification=VerificationResult("ambiguous", "Still loading"),
        post_frame_ids=("f-4",),
        recovery_attempted=True,
        recovery_success=False,
    )
    report = main(["--runs", str(recorder.jsonl_path), "--out", str(tmp_path / "report.json")])
    assert report["cases"] == 2
    assert report["geometry_accuracy"]["raw"]["all"] == {"ok": 1, "total": 2, "rate": 0.5}
    assert report["live_execution"]["raw"]["actuator_success"]["rate"] == 1.0
    assert report["live_execution"]["raw"]["verified_success"] == {"ok": 1, "total": 1, "rate": 1.0}
    assert report["live_execution"]["raw"]["verification_ambiguous"] == 1
    assert report["verification"] == {"ambiguous": 1, "success": 1}
    assert report["recovery"] == {"attempted": 1, "succeeded": 0, "failed": 1}
    assert report["candidate_coverage"]["ocr"]["ok"] == 0
    assert json.loads((tmp_path / "report.json").read_text()) == report


def test_evaluator_names_the_bad_line():
    good = json.dumps(_record().to_json())
    with pytest.raises(ValueError, match="runs.jsonl:2"):
        load_records([good, '{"schema_version": 1}'], source="runs.jsonl")
    assert evaluate(load_records([good]))["cases"] == 1
