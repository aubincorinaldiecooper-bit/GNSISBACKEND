from __future__ import annotations

import json
from dataclasses import replace

import pytest

from gnsis_runtime.visual.perception import (
    TargetGrounding,
    build_perception_prompt,
    parse_perception,
    parse_perception_or_grounding,
    perception_from_grounding,
    validate_target_point,
)


def test_perception_prompt_is_task_independent_and_bounds_visible_output() -> None:
    prompt = build_perception_prompt((1280, 720), temporal=True)

    assert "ordered from earliest to latest" in prompt
    assert "final image is the current view" in prompt
    assert "1280 by 720" in prompt
    assert "hidden content" in prompt
    assert "goal" not in prompt.lower()


def test_perception_prompt_can_focus_on_a_visible_question() -> None:
    prompt = build_perception_prompt(
        (1280, 720),
        temporal=True,
        focus="What is inside the magenta marker?",
    )

    assert "Focus question: What is inside the magenta marker?" in prompt
    assert "lower the confidence instead of guessing" in prompt


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


def test_perception_parser_normalizes_scalar_and_missing_array_fields() -> None:
    result = parse_perception(
        json.dumps(
            {
                "summary": "A product listing is visible.",
                "visible_text": "ESSENTIALS",
                "elements": {
                    "label": "Essentials item",
                    "role": "link",
                    "text": "ESSENTIALS",
                    "box": [10, 20, 300, 400],
                    "state": "visible",
                    "confidence": 0.7,
                },
                "changes": None,
                "confidence": 0.8,
            }
        ),
        frame_id="frame-4",
        observed_frame_ids=("frame-1", "frame-2", "frame-3", "frame-4"),
        motion=0.0,
        viewport=(1280, 942),
    )

    assert result.visible_text == ("ESSENTIALS",)
    assert result.changes == ()
    assert len(result.elements) == 1
    assert result.elements[0].label == "Essentials item"


def test_perception_prompt_passes_the_target_point_as_text_not_pixels() -> None:
    prompt = build_perception_prompt((1280, 720), temporal=False, target=(175, 387))

    assert "pointing at viewport pixel (175, 387)" in prompt
    assert "Nothing is drawn there" in prompt
    assert "box that contains the point" in prompt

    with pytest.raises(ValueError, match="outside the 1280x720 viewport"):
        build_perception_prompt((1280, 720), temporal=False, target=(1280, 10))
    with pytest.raises(ValueError, match="two integers"):
        validate_target_point(("a", 1), (1280, 720))


def test_perception_json_carries_target_grounding_with_provenance() -> None:
    perception = parse_perception(
        '{"summary":"A shop page.","visible_text":[],"elements":[],"changes":[],'
        '"confidence":0.7}',
        frame_id="f2",
        observed_frame_ids=("f1", "f2"),
        motion=0.1,
        viewport=(1280, 720),
    )
    assert perception.to_json()["grounding"] is None

    grounded = replace(
        perception,
        grounding=TargetGrounding(
            point=(175, 387),
            status="grounded",
            label="navy sweatpants",
            text="",
            box=(125, 123, 191, 370),
            confidence=0.7,
            source="florence-2-large-ft",
        ),
    )
    assert grounded.to_json()["grounding"] == {
        "point": {"x": 175, "y": 387},
        "status": "grounded",
        "label": "navy sweatpants",
        "text": "",
        "box": [125, 123, 191, 370],
        "confidence": 0.7,
        "source": "florence-2-large-ft",
    }


def test_perception_prompt_tells_the_model_not_to_echo_the_template() -> None:
    prompt = build_perception_prompt((1280, 720), temporal=False)

    assert "never return the placeholder words" in prompt


def test_grounded_target_can_stand_in_for_an_unparseable_perception() -> None:
    grounding = TargetGrounding(
        point=(175, 387),
        status="grounded",
        label="navy sweatpants",
        text="",
        box=(125, 123, 191, 370),
        confidence=0.7,
        source="florence-2-large-ft",
    )

    result = perception_from_grounding(
        grounding,
        frame_id="frame-4",
        observed_frame_ids=("frame-1", "frame-4"),
        motion=0.1,
        viewport=(1280, 942),
    )

    assert "Only the target point (175, 387) was resolved" in result.summary
    assert "rest of the frame was not described" in result.summary
    assert result.elements[0].box == (125, 123, 191, 370)
    assert result.confidence == 0.7
    assert result.frame_id == "frame-4"
    assert result.observed_frame_ids == ("frame-1", "frame-4")
    assert result.grounding is grounding
    assert result.to_json()["grounding"]["status"] == "grounded"


def test_unresolved_grounding_cannot_stand_in_for_a_perception() -> None:
    grounding = TargetGrounding(
        point=(5, 5),
        status="unresolved",
        label="",
        text="",
        box=None,
        confidence=0.0,
        source="florence-2-large-ft",
    )

    with pytest.raises(ValueError, match="only a grounded target"):
        perception_from_grounding(
            grounding,
            frame_id="frame-1",
            observed_frame_ids=("frame-1",),
            motion=0.0,
            viewport=(100, 100),
        )


def test_grounded_target_survives_a_template_echo_from_the_continuous_model() -> None:
    grounded = TargetGrounding(
        point=(5, 5),
        status="grounded",
        label="save button",
        text="Save",
        box=(0, 0, 10, 10),
        confidence=0.8,
        source="stub",
    )
    calls = 0

    def grounding() -> TargetGrounding:
        nonlocal calls
        calls += 1
        return grounded

    result = parse_perception_or_grounding(
        '{"summary":"plain-language overview","box":[x,y,width,height]}',
        grounding,
        frame_id="f2",
        observed_frame_ids=("f1", "f2"),
        motion=0.0,
        viewport=(64, 64),
    )

    assert calls == 1
    assert result.grounding == grounded
    assert result.elements[0].text == "Save"
    assert result.observed_frame_ids == ("f1", "f2")
    assert "Only the target point (5, 5) was resolved" in result.summary


def test_parse_failure_propagates_without_a_grounded_target() -> None:
    unresolved = TargetGrounding((5, 5), "unresolved", "", "", None, 0.0, "stub")
    provenance = {
        "frame_id": "f1",
        "observed_frame_ids": ("f1",),
        "motion": 0.0,
        "viewport": (64, 64),
    }

    with pytest.raises(ValueError, match="invalid perception JSON"):
        parse_perception_or_grounding("not json", lambda: unresolved, **provenance)
    with pytest.raises(ValueError, match="invalid perception JSON"):
        parse_perception_or_grounding("not json", None, **provenance)


def test_valid_perception_does_not_consult_the_grounding_fallback() -> None:
    def grounding() -> TargetGrounding:
        raise AssertionError("fallback must not run")

    result = parse_perception_or_grounding(
        json.dumps({"summary": "A page.", "visible_text": [], "elements": [], "changes": [], "confidence": 0.5}),
        grounding,
        frame_id="f1",
        observed_frame_ids=("f1",),
        motion=0.0,
        viewport=(64, 64),
    )

    assert result.summary == "A page."
    assert result.grounding is None
