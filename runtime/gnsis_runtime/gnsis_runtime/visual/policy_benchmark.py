"""Common offline scoring contract for bounded visual decision policies."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from .real_runs import Box, Point
from .schema import bounded_actions


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    case_id: str
    frame_id: str
    goal: str
    image_ref: str
    allowed_actions: tuple[str, ...]
    expected_action: str
    expect_abstain: bool = False
    target_box: Box | None = None
    task_id: str | None = None

    @classmethod
    def from_json(cls, data: Mapping[str, object]) -> "BenchmarkCase":
        target_box = data.get("target_box")
        return cls(
            case_id=str(data["case_id"]),
            frame_id=str(data["frame_id"]),
            goal=str(data["goal"]),
            image_ref=str(data["image_ref"]),
            allowed_actions=bounded_actions(
                tuple(str(value) for value in data["allowed_actions"])
            ),
            expected_action=str(data["expected_action"]),
            expect_abstain=bool(data.get("expect_abstain", False)),
            target_box=Box.from_json(target_box) if target_box is not None else None,
            task_id=str(data["task_id"]) if data.get("task_id") is not None else None,
        )

    def policy_input(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "frame_id": self.frame_id,
            "goal": self.goal,
            "image_ref": self.image_ref,
            "allowed_actions": list(self.allowed_actions),
        }


@dataclass(frozen=True, slots=True)
class PolicyResult:
    case_id: str
    frame_id: str
    action: str
    confidence: float
    target: Point | None = None
    actuator_success: bool | None = None
    task_success: bool | None = None

    @classmethod
    def from_json(cls, data: Mapping[str, object]) -> "PolicyResult":
        target = data.get("target")
        return cls(
            case_id=str(data["case_id"]),
            frame_id=str(data["frame_id"]),
            action=str(data["action"]),
            confidence=float(data["confidence"]),
            target=Point.from_json(target) if target is not None else None,
            actuator_success=data.get("actuator_success")
            if isinstance(data.get("actuator_success"), bool)
            else None,
            task_success=data.get("task_success")
            if isinstance(data.get("task_success"), bool)
            else None,
        )


def _ratio(ok: int, total: int) -> dict[str, int | float | None]:
    return {"ok": ok, "total": total, "rate": round(ok / total, 4) if total else None}


def score(
    cases: Iterable[BenchmarkCase], results: Iterable[PolicyResult]
) -> dict[str, object]:
    cases = tuple(cases)
    by_case = {result.case_id: result for result in results}
    counts = {
        "valid_action": [0, 0],
        "abstention": [0, 0],
        "action_selection": [0, 0],
        "grounding": [0, 0],
        "current_frame": [0, 0],
        "actuator": [0, 0],
        "task": [0, 0],
    }
    missing: list[str] = []
    for case in cases:
        result = by_case.get(case.case_id)
        if result is None:
            missing.append(case.case_id)
            continue
        valid = result.action == "wait" or result.action in case.allowed_actions
        counts["valid_action"][0] += int(valid)
        counts["valid_action"][1] += 1
        abstention_correct = (result.action == "wait") == case.expect_abstain
        counts["abstention"][0] += int(abstention_correct)
        counts["abstention"][1] += 1
        if not case.expect_abstain:
            counts["action_selection"][0] += int(result.action == case.expected_action)
            counts["action_selection"][1] += 1
        if case.target_box is not None:
            counts["grounding"][0] += int(
                result.target is not None and case.target_box.contains(result.target)
            )
            counts["grounding"][1] += 1
        counts["current_frame"][0] += int(result.frame_id == case.frame_id)
        counts["current_frame"][1] += 1
        if result.actuator_success is not None:
            counts["actuator"][0] += int(result.actuator_success)
            counts["actuator"][1] += 1
        if result.task_success is not None:
            counts["task"][0] += int(result.task_success)
            counts["task"][1] += 1
    return {
        "cases": len(cases),
        "missing": missing,
        "metrics": {
            name: _ratio(values[0], values[1]) for name, values in counts.items()
        },
    }


def load_jsonl(path: Path, factory):
    return tuple(
        factory(json.loads(line))
        for line in path.read_text().splitlines()
        if line.strip()
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--policy-inputs", type=Path)
    args = parser.parse_args()
    cases = load_jsonl(args.cases, BenchmarkCase.from_json)
    if args.policy_inputs:
        args.policy_inputs.write_text(
            "\n".join(json.dumps(case.policy_input()) for case in cases) + "\n"
        )
    results = load_jsonl(args.results, PolicyResult.from_json)
    print(json.dumps(score(cases, results), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
