"""Code-generated legal action set for one System-1 decision.

The model chooses; code decides what may be chosen. Every argument a decision
may carry - typed text, a URL, a scroll direction - is generated here from the
user's goal by the same candidate builder the JEV head points over, and the
target must be a point on the frame the set was built for. A decision is legal
only if it is one of these choices, so nothing a model emits can introduce a
value, address, frame or command that the goal and the current frame did not
already contain.

The same set is handed to every policy that competes for System 1 (see
``benchmark.py``), so they are compared on identical legal actions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .prompt import value_candidates
from .schema import (
    ActionName,
    Decision,
    DecisionError,
    bounded_actions,
    validate_decision,
)

TARGETED_ACTIONS = frozenset({"click", "type"})
ARGUMENT_FIELDS = ("text", "url", "direction")
_FIELD_FOR_KIND = {"text": "text", "url": "url", "direction": "direction"}
_ACTION_FOR_KIND: dict[str, ActionName] = {"text": "type", "url": "navigate", "direction": "scroll"}
_PLAIN_ACTIONS: tuple[ActionName, ...] = ("click", "back", "wait", "done", "recover")


class IllegalDecision(DecisionError):
    """A structurally valid decision that is not one of the legal choices."""


@dataclass(frozen=True, slots=True)
class LegalChoice:
    key: str
    action: ActionName
    text: str | None = None
    url: str | None = None
    direction: str | None = None

    @property
    def needs_target(self) -> bool:
        return self.action in TARGETED_ACTIONS

    def label(self) -> str:
        argument = self.text or self.url or self.direction
        return f"{self.action} {argument!r}" if argument else self.action

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"key": self.key, "action": self.action}
        for name in ARGUMENT_FIELDS:
            value = getattr(self, name)
            if value is not None:
                out[name] = value
        return out


@dataclass(frozen=True, slots=True)
class LegalActionSet:
    goal: str
    frame_id: str
    viewport: tuple[int, int]
    choices: tuple[LegalChoice, ...]

    def by_key(self) -> dict[str, LegalChoice]:
        return {choice.key: choice for choice in self.choices}

    @property
    def actions(self) -> frozenset[str]:
        return frozenset(choice.action for choice in self.choices)

    def match(self, decision: Decision) -> LegalChoice:
        """Return the choice a decision is, or raise if it is none of them."""

        validate_decision(decision, self.viewport)
        if decision.frame_id is not None and str(decision.frame_id) != self.frame_id:
            raise IllegalDecision(
                f"decision is bound to frame {decision.frame_id!r}, not the observed frame {self.frame_id!r}"
            )
        if decision.action not in self.actions:
            raise IllegalDecision(f"{decision.action!r} is not a legal action for this step")
        if decision.action in TARGETED_ACTIONS and decision.frame_id is None:
            raise IllegalDecision(f"{decision.action} target is not bound to the observed frame")
        arguments = {name: getattr(decision, name) for name in ARGUMENT_FIELDS}
        for choice in self.choices:
            if choice.action == decision.action and all(
                arguments[name] == getattr(choice, name) for name in ARGUMENT_FIELDS
            ):
                return choice
        supplied = {name: value for name, value in arguments.items() if value is not None}
        raise IllegalDecision(f"{decision.action} with {supplied!r} was not derived from the goal")

    def check(self, decision: Decision) -> str | None:
        """The reason a decision is illegal, or ``None`` when it is legal."""

        try:
            self.match(decision)
        except DecisionError as exc:
            return str(exc)
        return None

    def to_json(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "frame_id": self.frame_id,
            "viewport": list(self.viewport),
            "choices": [choice.to_json() for choice in self.choices],
        }


def legal_actions(
    goal: str,
    frame_id: str,
    viewport: tuple[int, int],
    allowed_actions: tuple[str, ...] | None = None,
) -> LegalActionSet:
    """Build the bounded legal set from the goal and the observed frame only."""

    goal = str(goal).strip()
    if not goal:
        raise ValueError("a legal action set needs a goal")
    width, height = viewport
    if width <= 0 or height <= 0:
        raise ValueError("viewport must be positive")
    allowed = set(bounded_actions(allowed_actions))
    allowed.add("wait")
    choices: list[LegalChoice] = []
    for candidate in value_candidates(goal):
        if candidate.kind not in _ACTION_FOR_KIND or candidate.value is None:
            continue
        action = _ACTION_FOR_KIND[candidate.kind]
        if action not in allowed:
            continue
        field_name = _FIELD_FOR_KIND[candidate.kind]
        index = sum(1 for choice in choices if choice.action == action)
        key = f"{action}:{candidate.value}" if action == "scroll" else f"{action}:{index}"
        choices.append(LegalChoice(key=key, action=action, **{field_name: candidate.value}))
    choices.extend(
        LegalChoice(key=action, action=action)
        for action in _PLAIN_ACTIONS
        if action in allowed
    )
    return LegalActionSet(goal=goal, frame_id=frame_id, viewport=(int(width), int(height)), choices=tuple(choices))
