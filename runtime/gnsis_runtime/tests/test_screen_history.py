from __future__ import annotations

import pytest

from gnsis_runtime.screen import LatestScreenFrameBuffer, ScreenFrame


def _frame(frame_id: str, captured_at_ms: int, source: str = "screen") -> ScreenFrame:
    return ScreenFrame(
        frame_id=frame_id,
        image=object(),
        captured_at_ms=captured_at_ms,
        metadata={"video_source": source, "encoding": "jpeg"},
    )


def test_consumed_frames_land_in_history_in_capture_order() -> None:
    buf = LatestScreenFrameBuffer(max_history_frames=8)
    buf.publish(_frame("f1", 100))
    buf.publish(_frame("f2", 200))
    buf.consume_for_unit()
    buf.publish(_frame("f3", 300))
    buf.consume_for_unit()
    assert [f.frame_id for f in buf.recent_frames()] == ["f3", "f2"]


def test_reuse_path_does_not_duplicate_history_entries() -> None:
    buf = LatestScreenFrameBuffer(max_history_frames=8)
    buf.publish(_frame("f1", 100))
    buf.consume_for_unit()
    # Static-screen omni units reuse the last frame — not a new observation.
    reused = buf.consume_for_unit(reuse_base=True)
    assert reused and reused[0].frame_id == "f1"
    assert len(buf.recent_frames()) == 1


def test_history_is_bounded_by_count() -> None:
    buf = LatestScreenFrameBuffer(max_history_frames=3)
    for i in range(6):
        buf.publish(_frame(f"f{i}", 100 * (i + 1)))
        buf.consume_for_unit()
    history = buf.recent_frames()
    assert [f.frame_id for f in history] == ["f5", "f4", "f3"]


def test_history_is_pruned_by_capture_time_window() -> None:
    buf = LatestScreenFrameBuffer(max_history_frames=10, history_window_ms=500)
    for i in range(4):
        buf.publish(_frame(f"f{i}", 1000 + 300 * i))
        buf.consume_for_unit()
    # newest=1900ms; cutoff=1400 → f0(1000), f1(1300) pruned
    assert [f.frame_id for f in buf.recent_frames()] == ["f3", "f2"]


def test_frame_at_or_before_selects_temporally() -> None:
    buf = LatestScreenFrameBuffer(max_history_frames=8)
    for i, ts in enumerate([1000, 2000, 3000]):
        buf.publish(_frame(f"f{i}", ts))
        buf.consume_for_unit()
    assert buf.frame_at_or_before(2500).frame_id == "f1"
    assert buf.frame_at_or_before(3000).frame_id == "f2"
    assert buf.frame_at_or_before(500) is None
    assert buf.latest_frame().frame_id == "f2"


def test_recent_frames_limit_and_within_ms() -> None:
    buf = LatestScreenFrameBuffer(max_history_frames=8)
    for i, ts in enumerate([1000, 1500, 2000, 2500]):
        buf.publish(_frame(f"f{i}", ts))
        buf.consume_for_unit()
    assert [f.frame_id for f in buf.recent_frames(limit=2)] == ["f3", "f2"]
    assert [f.frame_id for f in buf.recent_frames(within_ms=600)] == ["f3", "f2"]


def test_video_source_metadata_is_preserved_on_retained_frames() -> None:
    buf = LatestScreenFrameBuffer(max_history_frames=8)
    buf.publish(_frame("cam1", 100, source="camera"))
    buf.publish(_frame("scr1", 200, source="screen"))
    buf.consume_for_unit()
    buf.publish(_frame("cam2", 300, source="camera"))
    buf.consume_for_unit()
    history = buf.recent_frames()
    assert history[0].metadata["video_source"] == "camera"
    assert history[1].metadata["video_source"] == "screen"


def test_reset_clears_history_across_session_boundaries() -> None:
    buf = LatestScreenFrameBuffer(max_history_frames=8)
    buf.publish(_frame("f1", 100))
    buf.consume_for_unit()
    assert buf.latest_frame() is not None
    buf.reset()
    assert buf.latest_frame() is None
    assert buf.frame_at_or_before(10**9) is None
    assert buf.recent_frames() == ()


def test_history_disabled_when_bound_is_zero() -> None:
    buf = LatestScreenFrameBuffer(max_history_frames=0)
    buf.publish(_frame("f1", 100))
    buf.consume_for_unit()
    assert buf.latest_frame() is None


def test_invalid_history_bounds_rejected() -> None:
    with pytest.raises(ValueError):
        LatestScreenFrameBuffer(max_history_frames=-1)
    with pytest.raises(ValueError):
        LatestScreenFrameBuffer(history_window_ms=0)
