from __future__ import annotations

import asyncio
import json
from typing import Any

from gnsis_visual_sdk.browser_host import PlannerObservation
from gnsis_visual_sdk.model_planner import OpenRouterPlanner, _render_observation


def observation(**overrides: Any) -> PlannerObservation:
    defaults: dict[str, Any] = {
        "step": 1,
        "task": "Click the blue CLICK MARKER button.",
        "allowed_actions": ("click", "type", "scroll", "wait", "done"),
        "viewport": (1280, 960),
        "service_decision": {
            "action": "wait",
            "confidence": 0.43,
            "target": {"x": 0.1, "y": 0.2},
        },
        "history": (),
    }
    defaults.update(overrides)
    return PlannerObservation(**defaults)


def completion(content: str) -> dict[str, Any]:
    return {
        "model": "test/model",
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    }


def planner_with(response: dict[str, Any]) -> OpenRouterPlanner:
    planner = OpenRouterPlanner("test/model", "key")

    class FakeResponse:
        def raise_for_status(self) -> None:
            return

        def json(self) -> dict[str, Any]:
            return response

    class FakeClient:
        def __init__(self) -> None:
            self.payloads: list[dict[str, Any]] = []

        def post(self, _path: str, *, json: dict[str, Any]) -> FakeResponse:
            self.payloads.append(json)
            return FakeResponse()

        def close(self) -> None:
            return

    planner._client = FakeClient()  # type: ignore[assignment]
    return planner


def test_planner_returns_bounded_click_with_trace() -> None:
    planner = planner_with(
        completion(
            json.dumps(
                {
                    "action": "click",
                    "target": {"x": 0.12, "y": 0.04},
                    "why": "the button is near the top left",
                }
            )
        )
    )

    decision = asyncio.run(planner.plan(observation()))

    assert decision["action"] == "click"
    assert decision["target"] == {"x": 0.12, "y": 0.04}
    assert decision["trace"]["why"] == "the button is near the top left"
    assert planner.usage.requests == 1
    assert planner.usage.prompt_tokens == 11


def test_planner_falls_back_to_service_decision_on_illegal_action() -> None:
    planner = planner_with(completion(json.dumps({"action": "open_url"})))

    decision = asyncio.run(planner.plan(observation()))

    assert decision["action"] == "wait"
    assert decision["trace"]["fallback"] == "service_decision"


def test_planner_rejects_click_without_normalized_target() -> None:
    planner = planner_with(
        completion(json.dumps({"action": "click", "target": {"x": 640, "y": 39}}))
    )

    decision = asyncio.run(planner.plan(observation()))

    assert decision["action"] == "wait"
    assert decision["trace"]["fallback"] == "service_decision"


def test_observation_hides_everything_outside_the_contract() -> None:
    history = (
        {
            "step": 1,
            "executed_decision": {"action": "click", "target": {"x": 0.1, "y": 0.1}},
            "success": True,
            "message": "Clicked.",
            "evidence": {"target_box": {"x": 1, "y": 2, "width": 3, "height": 4}},
        },
    )

    rendered = json.loads(_render_observation(observation(history=history)))

    assert rendered["previous_steps"][0]["host_success"] is True
    assert "evidence" not in rendered["previous_steps"][0]
    assert "target_box" not in json.dumps(rendered)
