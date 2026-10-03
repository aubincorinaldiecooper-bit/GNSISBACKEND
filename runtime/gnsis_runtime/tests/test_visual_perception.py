from __future__ import annotations

import json

import pytest

from gnsis_runtime.visual.perception import (
    build_perception_prompt,
    parse_perception,
)


def test_perception_prompt_is_task_independent_and_bounds_visible_output() -> None:
    prompt = build_perception_prompt((1280, 720), temporal=True)

    assert "ordered from earliest to latest" in prompt
    assert "final image is the current view" in prompt
    assert "1280 by 720" in prompt
    assert "hidden content" in prompt
    assert "goal" not in prompt.lower()


def test_perception_parser_returns_agent_readable_scene_with_provenance() -> None:
    result = parse_perception(
        "```json\n"
        + json.dumps(
            {
                "summary": "A settings window is open.",
                "visible_text": ["Settings", "Save"],
                "elements": [
                    {
                        "label": "Save control",
                        "role": "button",
                        "text": "Save",
                        "box": [1200, 690, 200, 80],
                        "state": "enabled",
                        "confidence": 0.92,
                    },
                    {"label": "invalid", "box": ["bad"]},
                ],
                "changes": ["A confirmation dialog appeared."],
                "confidence": 1.5,
            }
        )
        + "\n```",
        frame_id="frame-2",
        observed_frame_ids=("frame-1", "frame-2"),
        motion=0.2,
        viewport=(1280, 720),
    )

    assert result.summary == "A settings window is open."
    assert result.visible_text == ("Settings", "Save")
    assert result.changes == ("A confirmation dialog appeared.",)
    assert result.confidence == 1.0
    assert result.frame_id == "frame-2"
    assert result.observed_frame_ids == ("frame-1", "frame-2")
    assert result.elements[0].box == (1200, 690, 80, 30)
    assert result.to_json()["viewport"] == {"width": 1280, "height": 720}


def test_perception_parser_rejects_unstructured_model_output() -> None:
    with pytest.raises(ValueError, match="invalid perception JSON"):
        parse_perception(
            "I see a settings page",
            frame_id="frame-1",
            observed_frame_ids=("frame-1",),
            motion=0.0,
            viewport=(1280, 720),
        )
