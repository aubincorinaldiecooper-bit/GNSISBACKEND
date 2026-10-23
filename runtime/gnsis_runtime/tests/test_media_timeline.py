from __future__ import annotations

import pytest

from gnsis_runtime.media_timeline import AudioCaptureTimeline, AudioFrameHeader


def _frame(sequence: int, start_sample: int, samples: int = 320) -> AudioFrameHeader:
    return AudioFrameHeader.from_payload(
        {
            "type": "audio.frame",
            "sequence": sequence,
            "start_sample": start_sample,
            "sample_count": samples,
            "captured_at_ms": 1_000 + start_sample // 16,
        }
    )


def test_counters_that_kept_running_from_an_earlier_call_set_the_baseline():
    timeline = AudioCaptureTimeline(sample_rate=16000, unit_ms=1000)
    pcm = b"\0" * 640
    timeline.split_frame(_frame(1, 0), pcm, max_part_bytes=640)
    timeline.reset()

    # A desktop that never reset its counters starts the next call at 1079.
    first = 1079
    start = (first - 1) * 320
    units = []
    for i in range(50):
        parts = timeline.split_frame(
            _frame(first + i, start + i * 320), pcm, max_part_bytes=640
        )
        units.extend(u for part in parts for u in part.unit_capture_start_ms)
    assert len(units) == 1
    assert units[0] == pytest.approx(1_000 + start / 16)


def test_a_gap_after_the_baseline_still_fails():
    timeline = AudioCaptureTimeline(sample_rate=16000, unit_ms=1000)
    pcm = b"\0" * 640
    timeline.split_frame(_frame(1079, 344_960), pcm, max_part_bytes=640)
    with pytest.raises(ValueError, match="sequence must be 1080, got 1082"):
        timeline.split_frame(_frame(1082, 345_600), pcm, max_part_bytes=640)
