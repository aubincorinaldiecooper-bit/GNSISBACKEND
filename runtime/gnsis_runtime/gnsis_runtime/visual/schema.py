"""Model-independent visual decision contract shared by GNSIS environment adapters."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from collections.abc import Iterable
from typing import Literal, cast

ACTIONS: tuple[str, ...] = (
    "click",
    "type",
    "scroll",
    "navigate",
    "back",
    "wait",
    "done",
    "recover",
)
ActionName = Literal[
    "click", "type", "scroll", "navigate", "back", "wait", "done", "recover"
]

TARGET_REQUIRED = frozenset({"click", "type"})
TARGET_OPTIONAL = frozenset({"recover"})
SCROLL_DIRECTIONS: tuple[str, ...] = ("down", "up")


@dataclass(frozen=True)
class Target:
    """Viewport CSS-pixel point, always inside the viewport."""

    x: int
    y: int


@dataclass(frozen=True)
class Decision:
    action: ActionName
    confidence: float
    target: Target | None = None
    text: str | None = None
    url: str | None = None
    direction: str | None = None
    frame_id: str | int | None = None
    timing_ms: dict[str, float] = field(default_factory=dict)

    def to_json(self) -> dict:
        out = {k: v for k, v in asdict(self).items() if v is not None and v != {}}
        out["confidence"] = round(self.confidence, 4)
        return out


class DecisionError(ValueError):
    pass


def bounded_actions(actions: Iterable[str] | None = None) -> tuple[ActionName, ...]:
    selected = tuple(dict.fromkeys(actions or ACTIONS))
    unknown = set(selected) - set(ACTIONS)
    if unknown:
        raise DecisionError(f"unknown actions: {sorted(unknown)}")
    if not selected:
        raise DecisionError("at least one action must be allowed")
    return cast(tuple[ActionName, ...], selected)


def validate_decision(
    decision: Decision,
    viewport: tuple[int, int],
    allowed_actions: tuple[str, ...] | None = None,
) -> Decision:
    """Reject decisions that the actuator must never execute."""
    width, height = viewport
    if decision.action not in ACTIONS:
        raise DecisionError(f"unknown action {decision.action!r}")
    legal = set(bounded_actions(allowed_actions))
    if decision.action != "wait" and decision.action not in legal:
        raise DecisionError(f"action {decision.action!r} is not allowed for this task")
    if not 0.0 <= decision.confidence <= 1.0:
        raise DecisionError("confidence must be in [0,1]")
    if decision.action in TARGET_REQUIRED and decision.target is None:
        raise DecisionError(f"{decision.action} requires a target")
    if decision.target is not None:
        if decision.action not in TARGET_REQUIRED | TARGET_OPTIONAL:
            raise DecisionError(f"{decision.action} does not take a target")
        if not (0 <= decision.target.x < width and 0 <= decision.target.y < height):
            raise DecisionError("target outside viewport")
    if decision.action == "type" and not decision.text:
        raise DecisionError("type requires text")
    if decision.action == "navigate" and not (decision.url or "").startswith(
        ("http://", "https://")
    ):
        raise DecisionError("navigate requires an http(s) url")
    if decision.action == "scroll" and decision.direction not in SCROLL_DIRECTIONS:
        raise DecisionError("scroll requires direction up|down")
    return decision


def decision_from_json(data: dict) -> Decision:
    target = data.get("target")
    return Decision(
        action=data["action"],
        confidence=float(data.get("confidence", 1.0)),
        target=Target(round(float(target["x"])), round(float(target["y"])))
        if target
        else None,
        text=data.get("text"),
        url=data.get("url"),
        direction=data.get("direction"),
    )
