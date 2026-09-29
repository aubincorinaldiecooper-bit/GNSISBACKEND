from pathlib import Path

import pytest

from gnsis_runtime.visual.real_runs import (
    BrowserExecutionEvidence,
    RealRunConsent,
    RealRunCoordinator,
    RealRunRecorder,
)


class AcceptingVerifier:
    def verify(self, **kwargs):
        assert kwargs["before_frame_id"] == "f-1"
        assert kwargs["post_frame_ids"] == ("f-2", "f-3")
        return True, "expected state appeared"


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
    recorder = RealRunRecorder(
        tmp_path,
        consent=RealRunConsent(local_collection=False, shared_training=False),
    )
    coordinator = RealRunCoordinator(recorder)
    record = coordinator.finalize(
        run_id="r1",
        case_id="c1",
        frame_id="f-1",
        goal="click continue",
        action="click",
        execution={},
        post_frame_ids=("f-2",),
    )
    assert record.verified_success is None
    assert not recorder.jsonl_path.exists()


def test_shared_export_requires_explicit_consent(tmp_path: Path):
    recorder = RealRunRecorder(
        tmp_path,
        consent=RealRunConsent(local_collection=True, shared_training=False),
    )
    coordinator = RealRunCoordinator(recorder)
    coordinator.finalize(
        run_id="r1",
        case_id="c1",
        frame_id="f-1",
        goal="click continue",
        action="click",
        execution={},
        post_frame_ids=("f-2",),
    )

    with pytest.raises(PermissionError):
        recorder.export_for_shared_training(tmp_path / "shared.jsonl")


def test_semantic_verifier_labels_post_action_run(tmp_path: Path):
    recorder = RealRunRecorder(
        tmp_path,
        consent=RealRunConsent(local_collection=True, shared_training=False),
    )
    coordinator = RealRunCoordinator(recorder, verifier=AcceptingVerifier())
    record = coordinator.finalize(
        run_id="r1",
        case_id="c1",
        frame_id="f-1",
        goal="click continue",
        action="click",
        execution={"success": True},
        post_frame_ids=("f-2", "f-3"),
        source_ref="/existing/screen/frame.jpg",
    )

    assert record.verified_success is True
    assert record.verification_reason == "expected state appeared"
    assert recorder.jsonl_path.exists()
    assert recorder.jsonl_path.read_text().count("\n") == 1


def test_no_post_frame_is_not_falsely_marked_failed(tmp_path: Path):
    recorder = RealRunRecorder(tmp_path)
    coordinator = RealRunCoordinator(recorder, verifier=AcceptingVerifier())
    record = coordinator.finalize(
        run_id="r1",
        case_id="c1",
        frame_id="f-1",
        goal="click continue",
        action="click",
        execution={},
        post_frame_ids=(),
    )
    assert record.verified_success is None
    assert record.verification_reason == "no post-action visual frame available"
