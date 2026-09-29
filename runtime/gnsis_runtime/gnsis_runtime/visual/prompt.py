"""Decision sequence layout for the JEV head.

The sequence contains the goal, bounded structured history, the current rendered
frame as visual tokens, candidate markers for actions and argument values, and
three query markers. No DOM-derived information is ever part of the sequence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .schema import ACTIONS, SCROLL_DIRECTIONS

CAND_MARK = "<|fim_suffix|>"
QUERY_MARK = "<|fim_middle|>"
QUERIES = ("action", "target", "value")
MAX_VALUES = 8
MAX_HISTORY = 6

_QUOTED = re.compile(r"[\"“”']([^\"“”']{1,80})[\"“”']")
_URL = re.compile(r"https?://[^\s\"'<>]+")


@dataclass(frozen=True)
class ValueCandidate:
    kind: str  # none | direction | text | url
    value: str | None


def value_candidates(goal: str) -> list[ValueCandidate]:
    """Bounded argument candidates derived from the goal only."""
    out = [ValueCandidate("none", None)] + [ValueCandidate("direction", d) for d in SCROLL_DIRECTIONS]
    urls = [u.rstrip(".,;)") for u in _URL.findall(goal)]
    for url in urls:
        out.append(ValueCandidate("url", url))
    for text in _QUOTED.findall(goal):
        if text not in urls:
            out.append(ValueCandidate("text", text))
    seen: set[tuple[str, str | None]] = set()
    unique = []
    for cand in out:
        key = (cand.kind, cand.value)
        if key not in seen:
            seen.add(key)
            unique.append(cand)
    return unique[:MAX_VALUES]


def format_history(history: list[dict]) -> str:
    if not history:
        return "none"
    lines = []
    for i, step in enumerate(history[-MAX_HISTORY:], 1):
        arg = step.get("text") or step.get("url") or step.get("direction") or ""
        lines.append(f"{i}. {step['action']}{' ' + repr(arg) if arg else ''}")
    return "; ".join(lines)


@dataclass(frozen=True)
class DecisionLayout:
    prefix: str
    suffix: str
    values: list[ValueCandidate]


def build_layout(goal: str, history: list[dict]) -> DecisionLayout:
    values = value_candidates(goal)
    prefix = f"<|im_start|>user\nGoal: {goal}\nDone so far: {format_history(history)}\nScreen:"
    actions = " ".join(f"{a}{CAND_MARK}" for a in ACTIONS)
    vals = " ".join(f"{c.value if c.value is not None else 'none'}{CAND_MARK}" for c in values)
    queries = " ".join(f"{q}{QUERY_MARK}" for q in QUERIES)
    suffix = f"\nActions: {actions}\nValues: {vals}\nNext {queries}<|im_end|>"
    return DecisionLayout(prefix=prefix, suffix=suffix, values=values)
