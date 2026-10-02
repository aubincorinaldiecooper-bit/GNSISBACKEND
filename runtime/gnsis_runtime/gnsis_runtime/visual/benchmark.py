"""Common System-1 benchmark: Smaller GNSIS against the Laya contract.

Both contestants are scored on identical inputs:

* the same goal, retained frames (oldest to newest, the last is current),
  structured history and stream motion (``Observation``);
* the same code-generated ``LegalActionSet`` built from that observation only;
* the same ``gate_decision`` - legal-set check, confidence abstention, motion
  settle and repeat guard - that the production decision session applies.

Oracle labels (``Oracle``) are split off when a case is loaded and are only
read by scoring. Nothing a contestant receives is derived from them.

Smaller GNSIS is the backend's JEV/MiniCPM-V policy behind the same
``VisualDecisionPolicy`` seam the decision session uses. The Laya contract is
the browser fork's Panoptic perception -> Laya bounded choice, rebuilt here
against the same legal set so it is projected onto the targets Panoptic
perceived and can only pick an offered key.

End-to-end episodes run each contestant inside the production
``PersistentVisualDecisionSession`` against an environment that publishes its
one persistent stream into a ``LatestScreenFrameBuffer`` and executes
``VisualStep`` objects - the existing ``StepExecutor`` seam - so no second
capture path or actuator is introduced.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import math
import sys
import time
import urllib.request
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence

from PIL import Image
from websockets.sync.client import connect as ws_connect

from ..screen import LatestScreenFrameBuffer, ScreenFrame
from .control import ExecutionReport, VisualStep, step_from_decision
from .legal import IllegalDecision, LegalActionSet, LegalChoice, legal_actions
from .real_runs import Box, Point
from .runtime import (
    MOTION_WINDOW_MS,
    DecisionGate,
    GatedDecision,
    PersistentVisualDecisionSession,
    RuntimeFrameView,
    VisualDecisionPolicy,
    gate_decision,
    recent_motion,
)
from .schema import Decision, DecisionError, Target

SMALLER_GNSIS = "smaller-gnsis"
LAYA_CONTRACT = "laya-contract"
LAYA_MAX_OPTIONS = 20
LAYA_MAX_TARGETS = 12
CALIBRATION_BINS = 10
COVERAGE_THRESHOLDS = (0.0, 0.3, 0.5, 0.7, 0.9)
Z_ONE_SIDED_95 = 1.645
DEFAULT_LAYA_URL = "http://127.0.0.1:8791"
DEFAULT_PANOPTIC_URL = "ws://127.0.0.1:8792/v1/panoptic/stream"
_TARGETED_ORACLE = frozenset({"click", "type", "recover"})


# --------------------------------------------------------------------------- cases


@dataclass(frozen=True)
class Observation:
    """Everything a contestant may see for one decision."""

    case_id: str
    goal: str
    frames: tuple[ScreenFrame, ...]
    history: tuple[Mapping[str, Any], ...] = ()
    motion: float | None = None

    def __post_init__(self) -> None:
        if not self.frames:
            raise ValueError("an observation needs at least one frame")
        if not str(self.goal).strip():
            raise ValueError("an observation needs a goal")

    @property
    def current(self) -> ScreenFrame:
        return self.frames[-1]

    @property
    def viewport(self) -> tuple[int, int]:
        image = self.current.image
        if not isinstance(image, Image.Image):
            raise TypeError("benchmark frames must contain PIL images")
        return image.size

    def stream_motion(self) -> float:
        """Motion recorded from the live stream, else measured on the frames."""

        return (
            float(self.motion)
            if self.motion is not None
            else recent_motion(self.frames)
        )

    def legal(self) -> LegalActionSet:
        return legal_actions(self.goal, self.current.frame_id, self.viewport)


@dataclass(frozen=True)
class Oracle:
    """Ground truth for one observation. Read by scoring only."""

    action: str
    value: str | None = None
    box: Box | None = None


@dataclass(frozen=True)
class BenchmarkCase:
    observation: Observation
    oracle: Oracle = field(repr=False)
    family: str = ""


def _box(value: Any) -> Box | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return Box.from_json(value)
    x, y, w, h = (float(v) for v in value)
    return Box(x, y, w, h)


def _load_frame(path: Path, frame_id: str, captured_at_ms: int | None) -> ScreenFrame:
    with Image.open(path) as image:
        rgb = image.convert("RGB")
    return ScreenFrame(
        frame_id=frame_id,
        image=rgb,
        captured_at_ms=captured_at_ms,
        metadata={"video_source": "benchmark"},
    )


def case_from_row(row: Mapping[str, Any], base: Path, index: int) -> BenchmarkCase:
    """Build a case from a native row or a browser ``collect.py`` state row.

    Native rows carry ``frames`` and an ``oracle`` object; collected rows carry
    one ``frame`` plus ``action``/``value``/``box`` labels at top level. Either
    way the labels go to ``Oracle`` and never into the ``Observation``.
    """

    if "frames" in row:
        case_id = str(row.get("case_id") or f"case-{index}")
        frames = tuple(
            _load_frame(
                base / str(item["path"]),
                str(item.get("frame_id") or f"{case_id}:{i}"),
                item.get("captured_at_ms"),
            )
            for i, item in enumerate(row["frames"])
        )
        label = row["oracle"]
    else:
        case_id = f"{row.get('episode', 'episode')}#{index}"
        frames = (_load_frame(base / str(row["frame"]), str(row["frame"]), None),)
        label = row
    observation = Observation(
        case_id=case_id,
        goal=str(row["goal"]),
        frames=frames,
        history=tuple(dict(step) for step in row.get("history") or ()),
        motion=float(row["motion"]) if row.get("motion") is not None else None,
    )
    oracle = Oracle(
        action=str(label["action"]),
        value=label.get("value"),
        box=_box(label.get("box")),
    )
    return BenchmarkCase(
        observation=observation, oracle=oracle, family=str(row.get("family") or "")
    )


def load_cases(
    path: str | Path, frames_dir: str | Path | None = None
) -> list[BenchmarkCase]:
    source = Path(path)
    base = Path(frames_dir) if frames_dir is not None else source.parent
    cases = []
    with source.open() as handle:
        for index, line in enumerate(handle):
            if line.strip():
                cases.append(case_from_row(json.loads(line), base, index))
    return cases


# ---------------------------------------------------------------------- contestants


class Contestant(Protocol):
    name: str

    def propose(self, observation: Observation, legal: LegalActionSet) -> Decision: ...


class PolicyContestant:
    """A ``VisualDecisionPolicy`` (Smaller GNSIS: the JEV engine) as a contestant."""

    def __init__(
        self,
        policy: VisualDecisionPolicy,
        *,
        cache: Any = None,
        name: str = SMALLER_GNSIS,
    ) -> None:
        self.policy = policy
        self.cache = cache
        self.name = name

    def propose(self, observation: Observation, legal: LegalActionSet) -> Decision:
        return self.policy.decide(
            RuntimeFrameView.from_screen_frame(observation.current),
            observation.goal,
            [dict(step) for step in observation.history],
            observation.stream_motion(),
            legal.viewport,
            self.cache,
            tuple(dict.fromkeys(choice.action for choice in legal.choices)),
        )


@dataclass(frozen=True)
class PerceivedTarget:
    id: str
    label: str
    role: str
    x: float
    y: float
    affordances: tuple[str, ...] = ()


@dataclass(frozen=True)
class PerceivedState:
    summary: str
    change: str
    page_stable: bool
    targets: tuple[PerceivedTarget, ...] = ()


def _json_object(text: str) -> dict[str, Any] | None:
    trimmed = text.strip()
    start, end = trimmed.find("{"), trimmed.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(trimmed[start : end + 1])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _text(value: Any, limit: int, default: str = "") -> str:
    return value.strip()[:limit] if isinstance(value, str) else default


def parse_visual_state(content: str) -> PerceivedState:
    """Port of the browser's ``parseVisualState`` for Panoptic responses."""

    value = _json_object(content)
    if value is None:
        return PerceivedState(
            summary=content.strip()[:2000], change="", page_stable=False
        )
    rows = value.get("targets")
    targets: list[PerceivedTarget] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        point = row.get("point")
        point = point if isinstance(point, dict) else {}
        x, y = _finite(point.get("x")), _finite(point.get("y"))
        target_id = _text(row.get("id"), 256)
        if not target_id or x is None or y is None or not (0 <= x <= 1 and 0 <= y <= 1):
            continue
        affordances = row.get("affordances")
        affordances = affordances if isinstance(affordances, list) else []
        targets.append(
            PerceivedTarget(
                id=target_id,
                label=_text(row.get("label"), 300),
                role=_text(row.get("role"), 80, "other"),
                x=x,
                y=y,
                affordances=tuple(
                    item.upper() for item in affordances if isinstance(item, str)
                )[:8],
            )
        )
        if len(targets) >= LAYA_MAX_TARGETS:
            break
    return PerceivedState(
        summary=_text(value.get("summary"), 2000),
        change=_text(value.get("change"), 1000),
        page_stable=value.get("page_stable") is True,
        targets=tuple(targets),
    )


@dataclass(frozen=True)
class LayaOption:
    key: str
    label: str
    decision: Decision


def _target_point(target: PerceivedTarget, legal: LegalActionSet) -> Target:
    width, height = legal.viewport
    return Target(
        min(width - 1, int(target.x * width)), min(height - 1, int(target.y * height))
    )


def laya_options(
    legal: LegalActionSet, perceived: PerceivedState
) -> dict[str, LayaOption]:
    """Laya's bounded option map, generated from the common legal set.

    Mirrors ``actionCandidates`` in the browser's LayaClient: per perceived
    target a click and one typing option per legal text, then the target-free
    legal choices, ``wait`` and ``done`` last, capped at 20 flat options.
    Every option is one of ``legal``'s choices, so Laya is offered exactly the
    commands and values Smaller GNSIS is, grounded on its own perception.
    """

    choices = legal.by_key()
    texts = [choice for choice in legal.choices if choice.action == "type"]
    frame_id = legal.frame_id
    options: list[LayaOption] = []
    for target in perceived.targets:
        affordances = set(target.affordances)
        point = _target_point(target, legal)
        name = target.label or target.id
        if "click" in choices and ("CLICK" in affordances or target.role != "input"):
            options.append(
                LayaOption(
                    f"click:{target.id}",
                    f'Click "{name}" ({target.role})',
                    Decision("click", 0.0, point, frame_id=frame_id),
                )
            )
        if "TYPE_TEXT" in affordances or target.role == "input":
            for index, choice in enumerate(texts):
                options.append(
                    LayaOption(
                        f"type:{target.id}:{index}",
                        f'Type "{choice.text}" into "{name}"',
                        Decision(
                            "type", 0.0, point, text=choice.text, frame_id=frame_id
                        ),
                    )
                )

    def plain(choice: LegalChoice, label: str) -> LayaOption:
        return LayaOption(
            choice.key,
            label,
            Decision(
                choice.action,
                0.0,
                text=choice.text,
                url=choice.url,
                direction=choice.direction,
                frame_id=frame_id,
            ),
        )

    for choice in legal.choices:
        if choice.action == "scroll":
            options.append(
                plain(
                    choice,
                    f"Scroll {choice.direction} to reveal more of the current page",
                )
            )
        elif choice.action == "navigate":
            options.append(
                plain(
                    choice,
                    f"Open the URL explicitly requested by the user: {choice.url}",
                )
            )
    if "back" in choices:
        options.append(plain(choices["back"], "Go back to the previous page"))
    if "recover" in choices:
        options.append(
            plain(choices["recover"], "Dismiss whatever is blocking the page")
        )
    tail = [
        plain(choices["wait"], "Wait for the visible browser state to change"),
        plain(
            choices["done"],
            "The user task is visibly complete; stop without another browser action",
        ),
    ]
    return {
        option.key: option
        for option in options[: max(0, LAYA_MAX_OPTIONS - len(tail))] + tail
    }


class Perceiver(Protocol):
    def perceive(self, observation: Observation) -> PerceivedState | None: ...


class Chooser(Protocol):
    def choose(
        self, state: Mapping[str, Any], criteria: Mapping[str, str]
    ) -> tuple[str, float]: ...


class LayaContractContestant:
    """Panoptic temporal perception -> Laya bounded choice, on the common legal set."""

    name = LAYA_CONTRACT

    def __init__(self, perceiver: Perceiver, chooser: Chooser) -> None:
        self.perceiver = perceiver
        self.chooser = chooser

    def propose(self, observation: Observation, legal: LegalActionSet) -> Decision:
        perceived = self.perceiver.perceive(observation)
        if perceived is None:
            # Panoptic stayed silent/standby: the browser agent keeps observing.
            return Decision("wait", 0.0, frame_id=legal.frame_id)
        options = laya_options(legal, perceived)
        state = {
            "task": observation.goal,
            "visual_summary": perceived.summary,
            "visual_change": perceived.change,
            "page_stable": perceived.page_stable,
            "tabs": [],
        }
        key, confidence = self.chooser.choose(
            state, {key: option.label for key, option in options.items()}
        )
        if key not in options or _finite(confidence) is None:
            raise IllegalDecision("Laya returned an invalid bounded action")
        return replace(options[key].decision, confidence=float(confidence))


class LayaHttpChooser:
    """The Laya ``/v1/systemone`` choice endpoint, called as LayaClient does."""

    def __init__(
        self, base_url: str = DEFAULT_LAYA_URL, *, timeout_s: float = 60.0
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def choose(
        self, state: Mapping[str, Any], criteria: Mapping[str, str]
    ) -> tuple[str, float]:
        body = json.dumps(
            {
                "state": dict(state),
                "questions": {
                    "action": {
                        "type": "choice",
                        "instructions": (
                            "Choose exactly one bounded browser action that best advances the user task "
                            "from the current Panoptic state. Never invent a target, value, URL, tab, or action."
                        ),
                        "criteria": dict(criteria),
                    }
                },
                "model": "laya",
            }
        ).encode()
        request = urllib.request.Request(
            f"{self.base_url}/v1/systemone",
            data=body,
            headers={"content-type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
            payload = json.loads(response.read() or b"{}")
        answer = (payload.get("answers") or {}).get("action") or {}
        choice = str(answer.get("choice") or "")
        confidence = answer.get("confidence")
        if confidence is None:
            confidence = (answer.get("probabilities") or {}).get(choice, 0.0)
        return choice, float(confidence)


def _jpeg_base64(frame: ScreenFrame) -> str:
    buffer = io.BytesIO()
    frame.image.convert("RGB").save(buffer, format="JPEG", quality=85)
    return base64.b64encode(buffer.getvalue()).decode()


def _history_event(step: Mapping[str, Any]) -> str:
    argument = step.get("text") or step.get("url") or step.get("direction") or ""
    return f"Executed {step.get('action')}{' ' + repr(argument) if argument else ''}"


class PanopticStreamPerceiver:
    """One Panoptic session per observation over its ordered frames.

    Frames are sent in capture order with the observation's history as
    timeline events. While Panoptic answers silence/standby the current frame
    is presented again, as a static page would keep appearing on a live
    stream, up to ``max_rounds``.
    """

    def __init__(
        self,
        url: str = DEFAULT_PANOPTIC_URL,
        *,
        token: str | None = None,
        max_rounds: int = 8,
        frame_interval_ms: int = 250,
    ) -> None:
        self.url = url
        self.token = token
        self.max_rounds = max_rounds
        self.frame_interval_ms = frame_interval_ms

    def perceive(self, observation: Observation) -> PerceivedState | None:
        stamps: list[int] = []
        for i, frame in enumerate(observation.frames):
            stamp = (
                frame.captured_at_ms
                if frame.captured_at_ms is not None
                else i * self.frame_interval_ms
            )
            stamps.append(max(int(stamp), stamps[-1] + 1) if stamps else int(stamp))
        with ws_connect(self.url, max_size=None) as socket:
            socket.send(
                json.dumps(
                    {
                        "type": "start",
                        "token": self.token,
                        "session_id": f"benchmark-{observation.case_id}",
                        "tab_id": 1,
                        "task": observation.goal,
                    }
                )
            )
            ready = json.loads(socket.recv())
            if ready.get("type") != "ready":
                raise RuntimeError(f"Panoptic did not start: {ready}")
            batch = [
                {
                    "frame_id": frame.frame_id,
                    "timestamp_ms": stamp,
                    "duration_ms": self.frame_interval_ms,
                    "image_base64": _jpeg_base64(frame),
                }
                for frame, stamp in zip(observation.frames, stamps)
            ]
            events = [
                {"time_ms": stamps[0], "content": _history_event(step)}
                for step in observation.history
            ]
            last = stamps[-1]
            for round_idx in range(self.max_rounds):
                socket.send(
                    json.dumps(
                        {"type": "batch", "epoch": 0, "frames": batch, "events": events}
                    )
                )
                message = json.loads(socket.recv())
                if message.get("type") == "error":
                    raise RuntimeError(f"Panoptic error: {message.get('reason')}")
                if (
                    message.get("type") == "temporal_state"
                    and message.get("state") == "response"
                ):
                    socket.send(json.dumps({"type": "end"}))
                    return parse_visual_state(str(message.get("content") or ""))
                last += self.frame_interval_ms
                batch = [
                    {
                        **batch[-1],
                        "frame_id": f"{observation.current.frame_id}:r{round_idx + 1}",
                        "timestamp_ms": last,
                    }
                ]
                events = []
            socket.send(json.dumps({"type": "end"}))
        return None


# -------------------------------------------------------------------------- scoring


def matches(decision: Decision | None, oracle: Oracle) -> bool:
    """Whether a decision is the oracle's action, argument and (if any) target."""

    if decision is None or decision.action != oracle.action:
        return False
    argument = {
        "type": decision.text,
        "navigate": decision.url,
        "scroll": decision.direction,
    }
    if (
        oracle.action in argument
        and oracle.value is not None
        and argument[oracle.action] != oracle.value
    ):
        return False
    if oracle.box is not None and oracle.action in _TARGETED_ORACLE:
        return decision.target is not None and oracle.box.contains(
            Point(decision.target.x, decision.target.y)
        )
    return True


def grounded(decision: Decision | None, oracle: Oracle) -> bool | None:
    """Target-in-box for oracle steps that have a target; ``None`` otherwise."""

    if oracle.box is None or oracle.action not in _TARGETED_ORACLE:
        return None
    if decision is None or decision.target is None:
        return False
    return oracle.box.contains(Point(decision.target.x, decision.target.y))


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    family: str
    contestant: str
    oracle_action: str
    gated: GatedDecision
    latency_ms: float
    correct: bool
    proposal_correct: bool
    grounded: bool | None

    @property
    def valid(self) -> bool:
        return self.gated.status != "rejected"

    @property
    def executed(self) -> Decision:
        return self.gated.decision

    def to_json(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "family": self.family,
            "contestant": self.contestant,
            "oracle_action": self.oracle_action,
            "gated": self.gated.to_json(),
            "latency_ms": self.latency_ms,
            "correct": self.correct,
            "proposal_correct": self.proposal_correct,
            "grounded": self.grounded,
        }


def run_case(
    contestant: Contestant, case: BenchmarkCase, gate: DecisionGate
) -> CaseResult:
    observation = case.observation
    legal = observation.legal()
    started = time.perf_counter()
    try:
        proposed: Any = contestant.propose(observation, legal)
    except DecisionError as exc:
        proposed = exc
    latency_ms = (time.perf_counter() - started) * 1000.0
    gated = gate_decision(
        proposed, legal, motion=observation.stream_motion(), gate=gate
    )
    return CaseResult(
        case_id=observation.case_id,
        family=case.family,
        contestant=contestant.name,
        oracle_action=case.oracle.action,
        gated=gated,
        latency_ms=latency_ms,
        correct=matches(gated.decision, case.oracle),
        proposal_correct=gated.status != "rejected"
        and matches(gated.proposed, case.oracle),
        grounded=grounded(
            gated.proposed if gated.status != "rejected" else None, case.oracle
        ),
    )


def _rate(ok: int, total: int) -> dict[str, Any]:
    return {"ok": ok, "total": total, "rate": ok / total if total else None}


def _percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))
    return ordered[index]


def calibration(
    pairs: Sequence[tuple[float, bool]], bins: int = CALIBRATION_BINS
) -> dict[str, Any]:
    """Expected calibration error, Brier score and a risk-coverage curve."""

    if not pairs:
        return {"n": 0, "ece": None, "brier": None, "coverage": []}
    n = len(pairs)
    buckets: list[list[tuple[float, bool]]] = [[] for _ in range(bins)]
    for confidence, ok in pairs:
        buckets[min(bins - 1, int(confidence * bins))].append((confidence, ok))
    ece = sum(
        len(bucket)
        / n
        * abs(
            sum(c for c, _ in bucket) / len(bucket)
            - sum(ok for _, ok in bucket) / len(bucket)
        )
        for bucket in buckets
        if bucket
    )
    brier = sum((confidence - float(ok)) ** 2 for confidence, ok in pairs) / n
    coverage = []
    for threshold in COVERAGE_THRESHOLDS:
        kept = [ok for confidence, ok in pairs if confidence >= threshold]
        coverage.append(
            {
                "threshold": threshold,
                "coverage": len(kept) / n,
                "selective_accuracy": sum(kept) / len(kept) if kept else None,
            }
        )
    return {"n": n, "ece": ece, "brier": brier, "coverage": coverage}


def summarize(results: Sequence[CaseResult]) -> dict[str, Any]:
    should_wait = [r for r in results if r.oracle_action == "wait"]
    should_act = [r for r in results if r.oracle_action != "wait"]
    grounding = [r.grounded for r in results if r.grounded is not None]
    per_action: dict[str, list[bool]] = {}
    per_family: dict[str, list[bool]] = {}
    for r in results:
        per_action.setdefault(r.oracle_action, []).append(r.correct)
        per_family.setdefault(r.family or "unlabelled", []).append(r.correct)
    rejected = [r for r in results if not r.valid]
    return {
        "cases": len(results),
        "validity": _rate(len(results) - len(rejected), len(results)),
        "rejections": [
            {"case_id": r.case_id, "reason": r.gated.reason} for r in rejected
        ][:50],
        "executed_invalid": sum(
            1 for r in results if not r.valid and r.executed.action != "wait"
        ),
        "accuracy": _rate(sum(r.correct for r in results), len(results)),
        "proposal_accuracy": _rate(
            sum(r.proposal_correct for r in results), len(results)
        ),
        "grounding": _rate(sum(grounding), len(grounding)),
        "abstention": {
            "should_wait_recall": _rate(
                sum(r.executed.action == "wait" for r in should_wait), len(should_wait)
            ),
            "false_abstention": _rate(
                sum(r.executed.action == "wait" for r in should_act), len(should_act)
            ),
            "gate_abstained": sum(r.gated.status == "abstain" for r in results),
        },
        "calibration": calibration(
            [
                (float(r.gated.proposed.confidence), r.proposal_correct)
                for r in results
                if r.valid and r.gated.proposed is not None
            ]
        ),
        "latency_ms": {
            "p50": _percentile([r.latency_ms for r in results], 0.5),
            "p95": _percentile([r.latency_ms for r in results], 0.95),
        },
        "per_action": {k: _rate(sum(v), len(v)) for k, v in sorted(per_action.items())},
        "per_family": {k: _rate(sum(v), len(v)) for k, v in sorted(per_family.items())},
    }


def paired_difference(
    candidate: Sequence[bool], incumbent: Sequence[bool]
) -> dict[str, Any]:
    """Candidate minus incumbent on the same items, with a one-sided 95% bound."""

    if len(candidate) != len(incumbent):
        raise ValueError("paired comparison needs the same items for both contestants")
    n = len(candidate)
    if n == 0:
        return {"n": 0, "difference": None, "lower_95": None}
    diffs = [float(a) - float(b) for a, b in zip(candidate, incumbent)]
    mean = sum(diffs) / n
    variance = sum((d - mean) ** 2 for d in diffs) / (n - 1) if n > 1 else 0.0
    return {
        "n": n,
        "difference": mean,
        "lower_95": mean - Z_ONE_SIDED_95 * math.sqrt(variance / n),
        "only_candidate": sum(1 for a, b in zip(candidate, incumbent) if a and not b),
        "only_incumbent": sum(1 for a, b in zip(candidate, incumbent) if b and not a),
    }


def case_digest(cases: Iterable[BenchmarkCase]) -> str:
    digest = hashlib.sha256()
    for case in cases:
        obs = case.observation
        digest.update(
            json.dumps(
                [
                    obs.case_id,
                    obs.goal,
                    [f.frame_id for f in obs.frames],
                    list(obs.history),
                ],
                default=str,
            ).encode()
        )
    return digest.hexdigest()


def run_cases(
    contestants: Sequence[Contestant],
    cases: Sequence[BenchmarkCase],
    gate: DecisionGate | None = None,
) -> dict[str, Any]:
    gate = gate if gate is not None else DecisionGate()
    results = {
        contestant.name: [run_case(contestant, case, gate) for case in cases]
        for contestant in contestants
    }
    report: dict[str, Any] = {
        "case_digest": case_digest(cases),
        "gate": gate.to_json(),
        "contestants": {name: summarize(rows) for name, rows in results.items()},
        "results": {
            name: [row.to_json() for row in rows] for name, rows in results.items()
        },
    }
    if SMALLER_GNSIS in results and LAYA_CONTRACT in results:
        ours, theirs = results[SMALLER_GNSIS], results[LAYA_CONTRACT]
        targeted = [i for i, row in enumerate(ours) if row.grounded is not None]
        report["paired"] = {
            "accuracy": paired_difference(
                [r.correct for r in ours], [r.correct for r in theirs]
            ),
            "grounding": paired_difference(
                [bool(ours[i].grounded) for i in targeted],
                [bool(theirs[i].grounded) for i in targeted],
            ),
        }
    return report


# ------------------------------------------------------------------------- episodes


class EpisodeEnvironment(Protocol):
    """A task environment behind the existing seams.

    It publishes its one persistent stream into ``screen_frames`` and executes
    steps as the ``StepExecutor`` does. ``succeeded`` is read by scoring only.
    """

    episode_id: str
    goal: str
    max_steps: int
    screen_frames: LatestScreenFrameBuffer

    def execute(self, step: VisualStep) -> ExecutionReport: ...

    def succeeded(self) -> bool: ...


class _ContestantPolicy:
    """Presents a contestant to the production decision session as its policy."""

    def __init__(
        self,
        contestant: Contestant,
        screen_frames: LatestScreenFrameBuffer,
        case_id: str,
    ) -> None:
        self.contestant = contestant
        self.screen_frames = screen_frames
        self.case_id = case_id
        self.name = contestant.name
        self.latencies: list[float] = []

    def decide(
        self,
        frame: Any,
        goal: str,
        history: list[dict],
        motion: float,
        viewport: tuple[int, int],
        cache: Any,
        allowed_actions: tuple[str, ...] | None = None,
    ) -> Decision:
        recent = tuple(
            reversed(self.screen_frames.recent_frames(within_ms=MOTION_WINDOW_MS))
        )
        if not recent:
            raise RuntimeError("no consumed visual frame is available")
        observation = Observation(
            case_id=f"{self.case_id}#{len(self.latencies)}",
            goal=goal,
            frames=recent,
            history=tuple(history),
            motion=motion,
        )
        started = time.perf_counter()
        try:
            return self.contestant.propose(
                observation,
                legal_actions(
                    goal,
                    frame.frame_id,
                    viewport,
                    allowed_actions,
                ),
            )
        finally:
            self.latencies.append((time.perf_counter() - started) * 1000.0)


@dataclass(frozen=True)
class EpisodeResult:
    episode_id: str
    contestant: str
    success: bool
    steps: int
    claimed_done: bool
    rejected: int
    abstained: int
    latency_ms: tuple[float, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "episode_id": self.episode_id,
            "contestant": self.contestant,
            "success": self.success,
            "steps": self.steps,
            "claimed_done": self.claimed_done,
            "rejected": self.rejected,
            "abstained": self.abstained,
        }


def run_episode(
    contestant: Contestant, env: EpisodeEnvironment, gate: DecisionGate | None = None
) -> EpisodeResult:
    policy = _ContestantPolicy(contestant, env.screen_frames, env.episode_id)
    session = PersistentVisualDecisionSession(policy, env.screen_frames, gate=gate)
    session.set_task(env.goal)
    rejected = abstained = steps = 0
    claimed_done = False
    for index in range(env.max_steps):
        gated = session.decide_gated()
        rejected += gated.status == "rejected"
        abstained += gated.status == "abstain"
        if gated.status == "act" and gated.decision.action == "done":
            claimed_done = True
            break
        env.execute(
            step_from_decision(
                gated,
                run_id=f"benchmark-{contestant.name}",
                case_id=f"{env.episode_id}#{index}",
                goal=env.goal,
            )
        )
        steps += 1
        if gated.status == "act":
            session.record_attempt(gated.decision)
    return EpisodeResult(
        episode_id=env.episode_id,
        contestant=contestant.name,
        success=bool(env.succeeded()),
        steps=steps,
        claimed_done=claimed_done,
        rejected=rejected,
        abstained=abstained,
        latency_ms=tuple(policy.latencies),
    )


def summarize_episodes(results: Sequence[EpisodeResult]) -> dict[str, Any]:
    latencies = [value for result in results for value in result.latency_ms]
    return {
        "episodes": len(results),
        "task_success": _rate(sum(r.success for r in results), len(results)),
        "false_done": sum(r.claimed_done and not r.success for r in results),
        "mean_steps": sum(r.steps for r in results) / len(results) if results else None,
        "rejected": sum(r.rejected for r in results),
        "abstained": sum(r.abstained for r in results),
        "latency_ms": {
            "p50": _percentile(latencies, 0.5),
            "p95": _percentile(latencies, 0.95),
        },
    }


# -------------------------------------------------------------- retirement gate


@dataclass(frozen=True)
class RetirementThresholds:
    min_cases: int = 500
    min_episodes: int = 100
    min_validity: float = 0.995
    non_inferiority_margin: float = 0.02
    max_ece: float = 0.10
    min_should_wait_recall: float = 0.90
    max_false_abstention: float = 0.10


def retirement_gate(
    cases_report: Mapping[str, Any],
    episodes_report: Mapping[str, Any] | None = None,
    thresholds: RetirementThresholds | None = None,
) -> dict[str, Any]:
    """Whether Smaller GNSIS has met the documented bar for retiring Laya.

    Every check must pass. A missing measurement fails its check: the gate
    never passes on evidence that was not collected.
    """

    t = thresholds if thresholds is not None else RetirementThresholds()
    contestants = cases_report.get("contestants") or {}
    ours: Mapping[str, Any] = contestants.get(SMALLER_GNSIS) or {}
    theirs: Mapping[str, Any] = contestants.get(LAYA_CONTRACT) or {}
    paired = cases_report.get("paired") or {}
    episodes = (episodes_report or {}).get("contestants") or {}
    ours_ep: Mapping[str, Any] = episodes.get(SMALLER_GNSIS) or {}
    paired_ep = (episodes_report or {}).get("paired") or {}
    checks: list[dict[str, Any]] = []

    def check(name: str, value: Any, threshold: Any, passed: bool) -> None:
        checks.append(
            {
                "name": name,
                "value": value,
                "threshold": threshold,
                "passed": bool(passed),
            }
        )

    def at_least(name: str, value: Any, threshold: float) -> None:
        check(name, value, f">= {threshold}", value is not None and value >= threshold)

    def at_most(name: str, value: Any, threshold: float) -> None:
        check(name, value, f"<= {threshold}", value is not None and value <= threshold)

    at_least(
        "paired_cases", min(ours.get("cases", 0), theirs.get("cases", 0)), t.min_cases
    )
    at_least("validity", (ours.get("validity") or {}).get("rate"), t.min_validity)
    check(
        "executed_invalid",
        ours.get("executed_invalid"),
        "== 0",
        ours.get("executed_invalid") == 0,
    )
    at_least(
        "accuracy_vs_laya_lower_95",
        (paired.get("accuracy") or {}).get("lower_95"),
        -t.non_inferiority_margin,
    )
    at_least(
        "grounding_vs_laya_lower_95",
        (paired.get("grounding") or {}).get("lower_95"),
        -t.non_inferiority_margin,
    )
    at_most("ece", (ours.get("calibration") or {}).get("ece"), t.max_ece)
    abstention = ours.get("abstention") or {}
    at_least(
        "should_wait_recall",
        (abstention.get("should_wait_recall") or {}).get("rate"),
        t.min_should_wait_recall,
    )
    at_most(
        "false_abstention",
        (abstention.get("false_abstention") or {}).get("rate"),
        t.max_false_abstention,
    )
    ours_p95 = (ours.get("latency_ms") or {}).get("p95")
    theirs_p95 = (theirs.get("latency_ms") or {}).get("p95")
    check(
        "latency_p95_ms_vs_laya",
        ours_p95,
        f"<= {theirs_p95}",
        ours_p95 is not None and theirs_p95 is not None and ours_p95 <= theirs_p95,
    )
    at_least(
        "paired_episodes",
        (paired_ep.get("task_success") or {}).get("n", 0),
        t.min_episodes,
    )
    at_least(
        "task_success_vs_laya_lower_95",
        (paired_ep.get("task_success") or {}).get("lower_95"),
        -t.non_inferiority_margin,
    )
    check(
        "false_done", ours_ep.get("false_done"), "== 0", ours_ep.get("false_done") == 0
    )
    return {"passed": all(item["passed"] for item in checks), "checks": checks}


def run_episodes(
    contestants: Sequence[Contestant], environments: Mapping[str, Any]
) -> dict[str, Any]:
    """Run every contestant on freshly built environments for each episode.

    ``environments`` maps an episode id to a zero-argument factory, so each
    contestant starts from the same initial state.
    """

    results = {
        contestant.name: [
            run_episode(contestant, factory()) for factory in environments.values()
        ]
        for contestant in contestants
    }
    report: dict[str, Any] = {
        "contestants": {
            name: summarize_episodes(rows) for name, rows in results.items()
        },
        "results": {
            name: [row.to_json() for row in rows] for name, rows in results.items()
        },
    }
    if SMALLER_GNSIS in results and LAYA_CONTRACT in results:
        report["paired"] = {
            "task_success": paired_difference(
                [r.success for r in results[SMALLER_GNSIS]],
                [r.success for r in results[LAYA_CONTRACT]],
            )
        }
    return report


# ----------------------------------------------------------------------------- CLI


def _smaller_gnsis(args: argparse.Namespace) -> Contestant:
    # Torch and the model stack load only when the JEV contestant is requested.
    from .backbone import BackboneConfig
    from .engine import JEVEngine, VisualCache

    engine = JEVEngine(
        BackboneConfig(model_dir=args.model, dtype=args.dtype, device=args.device),
        args.head,
    )
    return PolicyContestant(engine, cache=VisualCache())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Common System-1 benchmark: Smaller GNSIS against the Laya contract."
    )
    parser.add_argument(
        "--cases", required=True, help="JSONL of native or collect.py state rows"
    )
    parser.add_argument("--frames-dir", default=None)
    parser.add_argument(
        "--contestant",
        action="append",
        choices=(SMALLER_GNSIS, LAYA_CONTRACT),
        required=True,
    )
    parser.add_argument("--model", help="MiniCPM-V backbone path (smaller-gnsis)")
    parser.add_argument("--head", help="JEV head checkpoint (smaller-gnsis)")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--dtype", default="float32")
    parser.add_argument("--laya-url", default=DEFAULT_LAYA_URL)
    parser.add_argument("--panoptic-url", default=DEFAULT_PANOPTIC_URL)
    parser.add_argument("--panoptic-token", default=None)
    parser.add_argument(
        "--min-confidence", type=float, default=DecisionGate().min_confidence
    )
    parser.add_argument(
        "--episodes-report",
        default=None,
        help="JSON report from run_episodes, for the gate",
    )
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    contestants: list[Contestant] = []
    for name in dict.fromkeys(args.contestant):
        if name == SMALLER_GNSIS:
            if not args.model or not args.head:
                parser.error("smaller-gnsis needs --model and --head")
            contestants.append(_smaller_gnsis(args))
        else:
            contestants.append(
                LayaContractContestant(
                    PanopticStreamPerceiver(
                        args.panoptic_url, token=args.panoptic_token
                    ),
                    LayaHttpChooser(args.laya_url),
                )
            )
    report = run_cases(
        contestants,
        load_cases(args.cases, args.frames_dir),
        DecisionGate(min_confidence=args.min_confidence),
    )
    episodes = (
        json.loads(Path(args.episodes_report).read_text())
        if args.episodes_report
        else None
    )
    report["retirement_gate"] = retirement_gate(report, episodes)
    Path(args.out).write_text(json.dumps(report, indent=2))
    print(
        json.dumps(
            {
                "contestants": report["contestants"],
                "retirement_gate": report["retirement_gate"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
