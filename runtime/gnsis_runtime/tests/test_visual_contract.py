from __future__ import annotations

import pytest

from gnsis_runtime.visual.schema import Decision, DecisionError, Target, decision_from_json, validate_decision


VIEWPORT = (1280, 800)


@pytest.mark.parametrize(
    "decision",
    [
        Decision("click", 0.9),
        Decision("click", 0.9, Target(1280, 10)),
        Decision("type", 0.9, Target(10, 10)),
        Decision("navigate", 0.9, url="javascript:alert(1)"),
        Decision("scroll", 0.9, direction="left"),
        Decision("wait", 0.9, Target(10, 10)),
        Decision("click", 1.5, Target(10, 10)),
    ],
)
def test_visual_decision_contract_rejects_invalid_actions(decision):
    with pytest.raises(DecisionError):
        validate_decision(decision, VIEWPORT)


def test_visual_decision_round_trip():
    decision = Decision("type", 0.97, Target(742, 311), text="alice@example.com")
    back = validate_decision(decision_from_json(decision.to_json()), VIEWPORT)
    assert back.target == decision.target
    assert back.text == decision.text
    assert back.action == decision.action


def test_visual_frame_id_accepts_runtime_string_ids():
    decision = Decision("click", 0.9, Target(10, 10), frame_id="screen:42")
    assert validate_decision(decision, VIEWPORT).frame_id == "screen:42"
