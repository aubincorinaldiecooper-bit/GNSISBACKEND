"""Semantic visual verification: did the expected result visibly appear?

One contract for every environment. A verifier is given the frame an action was
decided on and the frames that came after it — taken from the shared screen
history, never from a new capture — plus the expected visible result that was
stated before acting. It answers ``success``, ``failure`` or ``ambiguous``.

What a verifier never does:

- treat the actuator's own report as success;
- treat "the pixels changed" as success;
- turn "cannot tell yet" into failure. ``failure`` needs something visible that
  contradicts the expected result, after an action that was carried out.

The things that actually look are *judges*. They are pluggable (text read from
the pixels, a vision-language model) and their output is untrusted: it is
validated here before it can become a result. Judges that need heavy model
dependencies import them only when they are used.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Mapping, Protocol, Sequence

from ..screen import ScreenFrame
from .runtime import recent_motion

VerificationStatus = Literal["success", "failure", "ambiguous"]
VERIFICATION_STATUSES: tuple[str, ...] = ("success", "failure", "ambiguous")

MAX_DESCRIPTION = 400
MAX_CUES = 8
MAX_CUE_LENGTH = 80
MAX_REASON = 300

DEFAULT_MIN_CONFIDENCE = 0.6
# Starting value for "the screen was still moving": recent_motion() of the last
# post-action frames. Recorded with every result so real runs can recalibrate it.
DEFAULT_UNSETTLED_MOTION = 0.35
DEFAULT_SETTLE_FRAMES = 3

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_EDGE_PUNCTUATION = ".,;:!?()[]{}\"'“”‘’«»"


def clean_text(text: Any, *, limit: int) -> str:
    value = _CONTROL.sub(" ", str(text)).strip()
    value = re.sub(r"\s+", " ", value)
    return value[:limit].rstrip()


def text_tokens(text: str) -> tuple[str, ...]:
    """Casefolded words with surrounding punctuation removed ("Price:" -> "price")."""

    words = (word.strip(_EDGE_PUNCTUATION) for word in str(text).casefold().split())
    return tuple(word for word in words if word)


def contains_phrase(haystack: Sequence[str], needle: Sequence[str]) -> bool:
    """Whether ``needle`` appears as whole consecutive words in ``haystack``.

    Whole words, so a cue "CAD" is not found inside "CADDY".
    """

    if not needle or len(needle) > len(haystack):
        return False
    width = len(needle)
    return any(tuple(haystack[i : i + width]) == tuple(needle) for i in range(len(haystack) - width + 1))


def _cues(values: Any, name: str) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        values = (values,)
    cues: list[str] = []
    for value in values:
        cue = clean_text(value, limit=MAX_CUE_LENGTH + 1)
        if not cue:
            continue
        if len(cue) > MAX_CUE_LENGTH:
            raise ValueError(f"{name} entries must be at most {MAX_CUE_LENGTH} characters")
        if not text_tokens(cue):
            raise ValueError(f"{name} entries must contain readable text")
        if cue not in cues:
            cues.append(cue)
    if len(cues) > MAX_CUES:
        raise ValueError(f"{name} accepts at most {MAX_CUES} entries")
    return tuple(cues)


@dataclass(frozen=True, slots=True)
class ExpectedState:
    """The visible result an action should produce, stated before acting.

    ``description`` is what a person would say ("Prices are shown in CAD").
    ``visible_text`` and ``absent_text`` are optional on-screen text cues: text
    that should be readable once the result holds, and text whose presence
    contradicts it ("USD" while expecting CAD). Cues let the result be checked
    by reading the pixels; the description is for judges that understand scenes.
    """

    description: str
    visible_text: tuple[str, ...] = ()
    absent_text: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        description = clean_text(self.description, limit=MAX_DESCRIPTION + 1)
        if not description:
            raise ValueError("an expected state needs a description")
        if len(description) > MAX_DESCRIPTION:
            raise ValueError(f"expected state description must be at most {MAX_DESCRIPTION} characters")
        visible = _cues(self.visible_text, "visible_text")
        absent = _cues(self.absent_text, "absent_text")
        if {text_tokens(c) for c in visible} & {text_tokens(c) for c in absent}:
            raise ValueError("the same text cannot be both expected and contrary")
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "visible_text", visible)
        object.__setattr__(self, "absent_text", absent)

    @classmethod
    def from_value(cls, value: Any) -> "ExpectedState | None":
        """Accept an ExpectedState, a plain description, or its JSON form."""

        if value is None:
            return None
        if isinstance(value, ExpectedState):
            return value
        if isinstance(value, str):
            return cls(value)
        if isinstance(value, Mapping):
            unknown = set(value) - {"description", "visible_text", "absent_text"}
            if unknown:
                raise ValueError(f"unknown expected state fields: {sorted(unknown)}")
            return cls(
                description=value.get("description") or "",
                visible_text=value.get("visible_text") or (),
                absent_text=value.get("absent_text") or (),
            )
        raise TypeError("expected state must be text or an object")

    def to_json(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "visible_text": list(self.visible_text),
            "absent_text": list(self.absent_text),
        }


def derive_expected_state(
    goal: str,
    action: str,
    *,
    text: str | None = None,
) -> ExpectedState | None:
    """An expected state for actions whose visible result follows from the action.

    Deliberately narrow. A click or a scroll has no visible result that can be
    known without understanding the page, so none is invented for them: the
    caller (System 2, or whoever planned the step) states it, or the outcome is
    recorded as ambiguous.
    """

    if action == "type" and text and text.strip():
        typed = clean_text(text, limit=MAX_CUE_LENGTH)
        cue = (typed,) if text_tokens(typed) else ()
        return ExpectedState(f'The typed text "{typed}" is visible where it was typed.', visible_text=cue)
    if action == "done" and goal and goal.strip():
        return ExpectedState(f"The goal is visibly complete: {clean_text(goal, limit=MAX_DESCRIPTION - 32)}")
    return None


@dataclass(frozen=True, slots=True)
class VerificationResult:
    status: VerificationStatus
    reason: str
    confidence: float | None = None
    judge: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in VERIFICATION_STATUSES:
            raise ValueError(f"unknown verification status {self.status!r}")
        reason = clean_text(self.reason, limit=MAX_REASON)
        if not reason:
            raise ValueError("a verification result needs a reason")
        object.__setattr__(self, "reason", reason)
        if self.confidence is not None:
            confidence = float(self.confidence)
            if not 0.0 <= confidence <= 1.0:
                raise ValueError("confidence must be in [0, 1]")
            object.__setattr__(self, "confidence", round(confidence, 4))

    @property
    def verified_success(self) -> bool | None:
        """True, False, or None for ambiguous — never a guess."""

        return {"success": True, "failure": False, "ambiguous": None}[self.status]

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "confidence": self.confidence,
            "judge": self.judge,
            "evidence": dict(self.evidence),
        }


def ambiguous(reason: str, **evidence: Any) -> VerificationResult:
    return VerificationResult("ambiguous", reason, None, None, evidence)


@dataclass(frozen=True, slots=True)
class VerificationRequest:
    """Everything a judge may look at for one executed action.

    ``before`` is the frame the action was decided on; ``after`` are frames from
    the shared history captured after it, oldest first. ``acted_at_ms`` is when
    the action started, on the same clock as the frames' ``captured_at_ms``;
    frames captured earlier are not evidence of its result. ``execution`` is
    the actuator's own report — used only to know whether the action was
    carried out, never as evidence of the result.
    """

    goal: str
    action: str
    expected_state: ExpectedState | None
    before: ScreenFrame
    after: tuple[ScreenFrame, ...]
    execution: Mapping[str, Any] = field(default_factory=dict)
    action_detail: Mapping[str, Any] = field(default_factory=dict)
    acted_at_ms: int | None = None


class VisualJudge(Protocol):
    """Looks at the frames and returns a verdict (untrusted until parsed)."""

    name: str

    def judge(self, request: VerificationRequest) -> Any: ...


class VisualVerifier(Protocol):
    def verify(self, request: VerificationRequest) -> VerificationResult: ...


_JSON_OBJECT = re.compile(r"\{.*?\}", re.DOTALL)


def parse_verdict(raw: Any) -> tuple[VerificationStatus, str, float | None, dict[str, Any]] | None:
    """Validate a judge's verdict. Anything malformed is None, never a label.

    Accepts a mapping or model text containing one JSON object with ``status``,
    ``reason`` and an optional ``confidence``.
    """

    data: Any = raw
    if isinstance(raw, str):
        data = None
        for match in _JSON_OBJECT.finditer(raw):
            try:
                candidate = json.loads(match.group(0))
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict) and "status" in candidate:
                data = candidate
                break
    if not isinstance(data, Mapping):
        return None
    status = data.get("status")
    if not isinstance(status, str) or status.strip().lower() not in VERIFICATION_STATUSES:
        return None
    reason = data.get("reason")
    if not isinstance(reason, str) or not clean_text(reason, limit=MAX_REASON):
        return None
    confidence = data.get("confidence")
    if confidence is not None:
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            return None
        confidence = float(confidence)
        if not 0.0 <= confidence <= 1.0:
            return None
    evidence = data.get("evidence")
    evidence = dict(evidence) if isinstance(evidence, Mapping) else {}
    return status.strip().lower(), clean_text(reason, limit=MAX_REASON), confidence, evidence  # type: ignore[return-value]


def actuator_reported_failure(execution: Mapping[str, Any]) -> bool:
    """Whether the actuator itself said the action did not happen."""

    if execution.get("actuator_success") is False:
        return True
    return bool(execution.get("error"))


def _after_action(frame: ScreenFrame, request: VerificationRequest) -> bool:
    if frame.frame_id == request.before.frame_id:
        return False
    threshold = request.acted_at_ms
    if threshold is None:
        threshold = request.before.captured_at_ms
    if threshold is None or frame.captured_at_ms is None:
        return True
    if request.acted_at_ms is not None:
        return frame.captured_at_ms >= threshold
    return frame.captured_at_ms > threshold


class SemanticVisualVerifier:
    """Decide success / failure / ambiguous from the actual frames.

    Guards run first and can only produce ``ambiguous``: no expected result, no
    frame from after the action, or a screen still moving. Then each judge is
    asked in order; the first confident, decisive verdict wins. A judge that
    errs, returns something unreadable, is unsure, or falls below the
    confidence floor leaves the question to the next one, and if none decides,
    the result is ``ambiguous`` with every judge's note.
    """

    def __init__(
        self,
        judges: Sequence[VisualJudge],
        *,
        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
        unsettled_motion: float = DEFAULT_UNSETTLED_MOTION,
        settle_frames: int = DEFAULT_SETTLE_FRAMES,
    ) -> None:
        if not 0.0 <= min_confidence <= 1.0:
            raise ValueError("min_confidence must be in [0, 1]")
        if unsettled_motion <= 0.0:
            raise ValueError("unsettled_motion must be positive")
        if settle_frames < 2:
            raise ValueError("settle_frames must be at least 2")
        self.judges = tuple(judges)
        self.min_confidence = float(min_confidence)
        self.unsettled_motion = float(unsettled_motion)
        self.settle_frames = int(settle_frames)

    def verify(self, request: VerificationRequest) -> VerificationResult:
        if request.expected_state is None:
            return ambiguous("No expected visible result was stated, so there is nothing to check the screen against.")
        after = tuple(frame for frame in request.after if _after_action(frame, request))
        if not after:
            return ambiguous("No frame from after the action was available.")
        tail = after[-self.settle_frames :]
        motion = round(recent_motion(tail, window_ms=10**9), 4) if len(tail) >= 2 else 0.0
        evidence = {"post_frame_ids": [frame.frame_id for frame in after], "motion": motion}
        if motion >= self.unsettled_motion:
            return ambiguous("The screen was still changing in the last frames after the action.", **evidence)

        request = replace(request, after=after)
        notes: list[str] = []
        for judge in self.judges:
            name = str(getattr(judge, "name", type(judge).__name__))
            try:
                raw = judge.judge(request)
            except Exception as exc:  # a judge failing is missing evidence, not a verdict
                notes.append(f"{name}: could not judge ({type(exc).__name__})")
                continue
            verdict = parse_verdict(raw)
            if verdict is None:
                notes.append(f"{name}: returned an unreadable verdict")
                continue
            status, reason, confidence, judge_evidence = verdict
            if status == "ambiguous":
                notes.append(f"{name}: {reason}")
                continue
            if confidence is None or confidence < self.min_confidence:
                notes.append(f"{name}: {status} with too little confidence ({confidence})")
                continue
            if status == "failure" and actuator_reported_failure(request.execution):
                notes.append(f"{name}: the result is not there, but the action was not carried out")
                continue
            return VerificationResult(status, reason, confidence, name, {**evidence, **judge_evidence})
        return VerificationResult(
            "ambiguous",
            "; ".join(notes) if notes else "No judge was available to look at the frames.",
            None,
            None,
            {**evidence, "notes": notes},
        )


class TextReaderLike(Protocol):
    def read(self, image: Any) -> list[Any]: ...


class OcrTextJudge:
    """Checks an expected state's text cues by reading the pixels.

    Uses the same pixel-only OCR as target snapping (RapidOCR). It never sees
    the DOM. It can only decide when the expected state names text:

    - success: every expected text is readable and no contrary text is;
    - failure: contrary text is readable and the expected text is not;
    - ambiguous: anything else (mixed, or simply not visible yet).
    """

    name = "ocr-text"
    # Confidence when success rests only on contrary text being absent: OCR not
    # finding text is weaker evidence than OCR finding it.
    ABSENCE_CONFIDENCE = 0.8

    def __init__(self, reader: TextReaderLike | None = None) -> None:
        self._reader = reader

    @property
    def reader(self) -> TextReaderLike:
        if self._reader is None:
            from .ocr import TextReader

            self._reader = TextReader()
        return self._reader

    @staticmethod
    def _find(cue: str, boxes: Sequence[Any]) -> Any | None:
        needle = text_tokens(cue)
        best = None
        for box in boxes:
            if contains_phrase(text_tokens(getattr(box, "text", "")), needle):
                if best is None or float(getattr(box, "score", 0.0)) > float(getattr(best, "score", 0.0)):
                    best = box
        return best

    def judge(self, request: VerificationRequest) -> dict[str, Any]:
        expected = request.expected_state
        if expected is None or not (expected.visible_text or expected.absent_text):
            return {"status": "ambiguous", "reason": "No on-screen text was named for this result."}
        before_boxes = list(self.reader.read(request.before.image))
        boxes = list(self.reader.read(request.after[-1].image))
        found = {cue: self._find(cue, boxes) for cue in expected.visible_text}
        contrary = {cue: self._find(cue, boxes) for cue in expected.absent_text}
        contrary_before = {cue: self._find(cue, before_boxes) for cue in expected.absent_text}
        missing = [cue for cue, box in found.items() if box is None]
        shown_contrary = [cue for cue, box in contrary.items() if box is not None]
        evidence: dict[str, Any] = {
            "read_frame_id": request.after[-1].frame_id,
            "found": {cue: getattr(box, "text", None) for cue, box in found.items() if box is not None},
            "missing": missing,
            "contrary": {cue: getattr(contrary[cue], "text", None) for cue in shown_contrary},
        }
        if not missing and not shown_contrary:
            if expected.visible_text:
                evidence["already_visible_before"] = all(self._find(cue, before_boxes) for cue in expected.visible_text)
                confidence = min(float(getattr(box, "score", 0.0)) for box in found.values())
                shown = ", ".join(f'"{cue}"' for cue in expected.visible_text)
                note = " (it was already visible before the action)" if evidence["already_visible_before"] else ""
                return {"status": "success", "reason": f"Now visible: {shown}{note}.", "confidence": confidence, "evidence": evidence}
            previously_visible = [cue for cue, box in contrary_before.items() if box is not None]
            evidence["contrary_visible_before"] = previously_visible
            # OCR failing to read a cue after the action is not proof that the
            # cue disappeared. For absence-only expectations, require that the
            # same contrary text was actually readable before the action.
            if set(previously_visible) != set(expected.absent_text):
                absent = ", ".join(f'"{cue}"' for cue in expected.absent_text)
                return {
                    "status": "ambiguous",
                    "reason": f"Could not prove that {absent} disappeared because it was not reliably readable before the action.",
                    "evidence": evidence,
                }
            absent = ", ".join(f'"{cue}"' for cue in expected.absent_text)
            confidence = min(float(getattr(contrary_before[cue], "score", 0.0)) for cue in expected.absent_text)
            return {
                "status": "success",
                "reason": f"No longer visible: {absent}.",
                "confidence": min(self.ABSENCE_CONFIDENCE, confidence),
                "evidence": evidence,
            }
        if shown_contrary and (missing or not expected.visible_text):
            confidence = max(float(getattr(contrary[cue], "score", 0.0)) for cue in shown_contrary)
            still = ", ".join(f'"{cue}"' for cue in shown_contrary)
            gone = f"; {', '.join(repr(cue) for cue in missing)} not visible" if missing else ""
            return {"status": "failure", "reason": f"Contrary text is visible: {still}{gone}.", "confidence": confidence, "evidence": evidence}
        if shown_contrary:
            return {"status": "ambiguous", "reason": "Both the expected text and contrary text are visible.", "evidence": evidence}
        waiting = ", ".join(f'"{cue}"' for cue in missing)
        return {"status": "ambiguous", "reason": f"Not visible yet: {waiting}; nothing visible contradicts it.", "evidence": evidence}
