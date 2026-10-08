"""HiPLEX-style event-causal scoring for GNSIS/Gander timeline traces.

This module does not copy HiPLEX's {pad, epad, cont} action vocabulary.
Gander already has richer learned actions: listen, speak, backchannel and
interrupt. We reuse the event-causal credit-assignment idea and preserve
Gander's native actions.

The scorer consumes the existing shared SessionTimeline JSONL plus a small
annotation file describing conversational events. It never requires raw
microphone audio and never creates a parallel audit log.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence


ACTION_MAP: dict[str, dict[str, Any]] = {
    "listen": {
        "hiplex_group": "pad_like",
        "emits_content": False,
        "timing_role": "hold_floor",
    },
    "speak": {
        "hiplex_group": "cont_like",
        "emits_content": True,
        "timing_role": "sustained_speech",
    },
    "backchannel": {
        "hiplex_group": "cont_like",
        "emits_content": True,
        "timing_role": "short_acknowledgement",
    },
    "interrupt": {
        "hiplex_group": "explicit_yield",
        "emits_content": False,
        "timing_role": "yield_floor",
    },
    "tool": {
        "hiplex_group": "excluded",
        "emits_content": False,
        "timing_role": "non_conversational",
    },
}

CONVERSATIONAL_ACTIONS = frozenset({"listen", "speak", "backchannel", "interrupt"})
SPEECH_ACTIONS = frozenset({"speak", "backchannel"})
TERMINAL_PLAYBACK_KINDS = frozenset({"playback.completed", "playback.cancelled"})


@dataclass(frozen=True)
class Decision:
    seq: int
    ts_ms: int
    action: str
    fields: dict[str, Any]


@dataclass(frozen=True)
class PlaybackSpan:
    playback_id: str
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class SpeechEpisode:
    start_ms: int
    end_ms: int
    playback_ids: tuple[str, ...]

    @property
    def duration_ms(self) -> int:
        return max(0, self.end_ms - self.start_ms)


def map_gander_action(action: str) -> dict[str, Any]:
    """Return the HiPLEX-compatible role without changing Gander's action space."""
    key = str(action or "").strip().lower()
    if key not in ACTION_MAP:
        raise ValueError(f"unknown Gander action: {action!r}")
    return {"action": key, **ACTION_MAP[key]}


def _event_ts(event: dict[str, Any], clock: str) -> int:
    if clock == "source":
        value = event.get("source_ts_ms")
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    value = event.get("recv_ts_ms")
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("timeline event is missing integer recv_ts_ms")
    return value


def load_timeline(path: str | Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line_no, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError(f"timeline line {line_no} is not an object")
        events.append(value)
    return sorted(events, key=lambda event: int(event.get("seq") or 0))


def load_annotations(path: str | Path) -> list[dict[str, Any]]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(value, dict):
        value = value.get("events")
    if not isinstance(value, list):
        raise ValueError("annotation file must be a JSON list or {events: [...]}")
    annotations = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"annotation {index} is not an object")
        event_type = str(item.get("type") or "").strip().lower()
        if event_type not in {"turn", "pause", "backchannel", "interruption"}:
            raise ValueError(f"annotation {index} has unsupported type {event_type!r}")
        annotations.append(dict(item, type=event_type))
    return annotations


def extract_decisions(
    events: Sequence[dict[str, Any]], *, clock: str = "recv"
) -> list[Decision]:
    decisions: list[Decision] = []
    for event in events:
        if event.get("kind") != "foreground.decision":
            continue
        fields = event.get("fields") or {}
        if not isinstance(fields, dict):
            continue
        action = str(fields.get("action") or "").strip().lower()
        if action not in ACTION_MAP:
            continue
        decisions.append(
            Decision(
                seq=int(event.get("seq") or 0),
                ts_ms=_event_ts(event, clock),
                action=action,
                fields=dict(fields),
            )
        )
    return decisions


def extract_playback_spans(
    events: Sequence[dict[str, Any]], *, clock: str = "recv"
) -> tuple[list[PlaybackSpan], int]:
    starts: dict[str, int] = {}
    spans: list[PlaybackSpan] = []
    incomplete = 0
    for event in events:
        kind = str(event.get("kind") or "")
        fields = event.get("fields") or {}
        if not isinstance(fields, dict):
            continue
        playback_id = fields.get("playback_id") or event.get("correlation_id")
        if not isinstance(playback_id, str) or not playback_id:
            continue
        ts_ms = _event_ts(event, clock)
        if kind == "playback.started":
            starts[playback_id] = ts_ms
            continue
        if kind not in TERMINAL_PLAYBACK_KINDS:
            continue
        start_ms = starts.pop(playback_id, None)
        if start_ms is None:
            continue
        spans.append(
            PlaybackSpan(
                playback_id=playback_id,
                start_ms=start_ms,
                end_ms=max(start_ms, ts_ms),
            )
        )
    incomplete = len(starts)
    spans.sort(key=lambda item: (item.start_ms, item.end_ms))
    return spans, incomplete


def merge_speech_episodes(
    spans: Sequence[PlaybackSpan], *, max_gap_ms: int = 1000
) -> list[SpeechEpisode]:
    if max_gap_ms < 0:
        raise ValueError("max_gap_ms must be non-negative")
    episodes: list[SpeechEpisode] = []
    for span in sorted(spans, key=lambda item: (item.start_ms, item.end_ms)):
        if not episodes or span.start_ms - episodes[-1].end_ms > max_gap_ms:
            episodes.append(
                SpeechEpisode(
                    start_ms=span.start_ms,
                    end_ms=span.end_ms,
                    playback_ids=(span.playback_id,),
                )
            )
            continue
        previous = episodes[-1]
        episodes[-1] = SpeechEpisode(
            start_ms=previous.start_ms,
            end_ms=max(previous.end_ms, span.end_ms),
            playback_ids=(*previous.playback_ids, span.playback_id),
        )
    return episodes


def _overlap_ms(start_a: int, end_a: int, start_b: int, end_b: int) -> int:
    return max(0, min(end_a, end_b) - max(start_a, start_b))


def _decisions_between(
    decisions: Sequence[Decision],
    start_ms: int,
    end_ms: int,
    *,
    actions: Iterable[str] | None = None,
) -> list[Decision]:
    allowed = None if actions is None else frozenset(actions)
    return [
        item
        for item in decisions
        if start_ms <= item.ts_ms <= end_ms
        and (allowed is None or item.action in allowed)
    ]


def _target(decision: Decision, *, reason: str, polarity: str) -> dict[str, Any]:
    mapped = map_gander_action(decision.action)
    return {
        "seq": decision.seq,
        "ts_ms": decision.ts_ms,
        "action": decision.action,
        "hiplex_group": mapped["hiplex_group"],
        "polarity": polarity,
        "reason": reason,
    }


def _nearest_decision(
    decisions: Sequence[Decision],
    ts_ms: int,
    *,
    actions: Iterable[str] | None = None,
    lookback_ms: int = 1500,
) -> Decision | None:
    candidates = _decisions_between(
        decisions, ts_ms - lookback_ms, ts_ms, actions=actions
    )
    return candidates[-1] if candidates else None


def _score_turn(
    ann: dict[str, Any],
    decisions: Sequence[Decision],
    episodes: Sequence[SpeechEpisode],
) -> dict[str, Any]:
    anchor = int(ann.get("anchor_ms", ann.get("end_ms")))
    window_start = int(ann.get("start_ms", anchor - 2000))
    window_end = int(ann.get("window_end_ms", anchor + 5000))
    candidate = next(
        (
            ep
            for ep in episodes
            if ep.end_ms > window_start and ep.start_ms <= window_end
            and ep.end_ms > anchor
        ),
        None,
    )
    targets: list[dict[str, Any]] = []
    latency = None
    early_ms = 0
    if candidate is not None:
        latency = candidate.start_ms - anchor
        early_ms = max(0, anchor - candidate.start_ms)
        onset_decision = _nearest_decision(
            decisions,
            candidate.start_ms,
            actions=SPEECH_ACTIONS,
        )
        if onset_decision is not None:
            targets.append(
                _target(
                    onset_decision,
                    reason="turn_response_onset",
                    polarity="positive" if latency >= 0 else "negative",
                )
            )
        if early_ms:
            for item in _decisions_between(
                decisions, candidate.start_ms, anchor, actions=SPEECH_ACTIONS
            ):
                targets.append(
                    _target(item, reason="turn_early_start", polarity="negative")
                )
    else:
        for item in _decisions_between(
            decisions, anchor, window_end, actions={"listen"}
        ):
            targets.append(
                _target(item, reason="turn_waiting_without_response", polarity="negative")
            )
    return {
        "trainable": bool(targets),
        "metrics": {
            "response_onset_ms": None if candidate is None else candidate.start_ms,
            "response_latency_ms": latency,
            "early_speech_ms": early_ms,
        },
        "causal_targets": targets,
    }


def _score_pause(
    ann: dict[str, Any],
    decisions: Sequence[Decision],
    episodes: Sequence[SpeechEpisode],
    *,
    short_episode_ms: int,
) -> dict[str, Any]:
    start = int(ann["start_ms"])
    end = int(ann["end_ms"])
    overlaps = [
        _overlap_ms(ep.start_ms, ep.end_ms, start, end) for ep in episodes
    ]
    intrusion_ms = sum(overlaps)
    sustained = any(
        overlap > 0 and ep.duration_ms > short_episode_ms
        for ep, overlap in zip(episodes, overlaps)
    )
    targets = [
        _target(item, reason="pause_intrusion", polarity="negative")
        for item in _decisions_between(decisions, start, end, actions=SPEECH_ACTIONS)
    ]
    if intrusion_ms == 0:
        targets.extend(
            _target(item, reason="pause_hold", polarity="positive")
            for item in _decisions_between(decisions, start, end, actions={"listen"})
        )
    return {
        "trainable": bool(targets),
        "metrics": {
            "pause_duration_ms": max(0, end - start),
            "intrusion_ms": intrusion_ms,
            "sustained_intrusion": sustained,
        },
        "causal_targets": targets,
    }


def _score_backchannel(
    ann: dict[str, Any],
    decisions: Sequence[Decision],
    episodes: Sequence[SpeechEpisode],
    *,
    short_episode_ms: int,
    tolerance_ms: int,
) -> dict[str, Any]:
    opportunity = int(
        ann.get(
            "opportunity_ms",
            (int(ann["start_ms"]) + int(ann["end_ms"])) // 2,
        )
    )
    short = [ep for ep in episodes if ep.duration_ms <= short_episode_ms]
    nearest = min(
        short,
        key=lambda ep: abs(ep.start_ms - opportunity),
        default=None,
    )
    distance = None if nearest is None else abs(nearest.start_ms - opportunity)
    matched = nearest is not None and distance is not None and distance <= tolerance_ms
    targets: list[dict[str, Any]] = []
    if matched and nearest is not None:
        candidates = _decisions_between(
            decisions,
            nearest.start_ms - tolerance_ms,
            nearest.end_ms + tolerance_ms,
            actions={"backchannel"},
        )
        if not candidates:
            candidates = _decisions_between(
                decisions,
                nearest.start_ms - tolerance_ms,
                nearest.end_ms + tolerance_ms,
                actions=SPEECH_ACTIONS,
            )
        targets.extend(
            _target(item, reason="backchannel_match", polarity="positive")
            for item in candidates
        )
    else:
        start = int(ann.get("start_ms", opportunity - tolerance_ms))
        end = int(ann.get("end_ms", opportunity + tolerance_ms))
        targets.extend(
            _target(item, reason="backchannel_missed_opportunity", polarity="negative")
            for item in _decisions_between(decisions, start, end, actions={"listen"})
        )
    overlong = any(
        abs(ep.start_ms - opportunity) <= tolerance_ms
        and ep.duration_ms > short_episode_ms
        for ep in episodes
    )
    if overlong:
        start = opportunity - tolerance_ms
        end = opportunity + tolerance_ms + short_episode_ms
        targets.extend(
            _target(item, reason="backchannel_overlong", polarity="negative")
            for item in _decisions_between(decisions, start, end, actions={"speak"})
        )
    return {
        "trainable": bool(targets),
        "metrics": {
            "matched": matched,
            "nearest_short_episode_distance_ms": distance,
            "overlong": overlong,
        },
        "causal_targets": targets,
    }


def _score_interruption(
    ann: dict[str, Any],
    decisions: Sequence[Decision],
    episodes: Sequence[SpeechEpisode],
    *,
    grace_ms: int,
) -> dict[str, Any]:
    start = int(ann["start_ms"])
    end = int(ann["end_ms"])
    post_grace = start + grace_ms
    overlap_ms = sum(
        _overlap_ms(ep.start_ms, ep.end_ms, post_grace, end) for ep in episodes
    )
    overlapping = [
        ep for ep in episodes if _overlap_ms(ep.start_ms, ep.end_ms, start, end) > 0
    ]
    last_audible = max((min(ep.end_ms, end) for ep in overlapping), default=None)
    yield_latency = (
        None if last_audible is None else max(0, last_audible - start)
    )
    reentry = next((ep for ep in episodes if ep.start_ms >= end), None)
    targets: list[dict[str, Any]] = []
    yield_decisions = _decisions_between(
        decisions, start, end, actions={"interrupt", "listen"}
    )
    if overlap_ms == 0:
        targets.extend(
            _target(item, reason="interruption_yield", polarity="positive")
            for item in yield_decisions
        )
    else:
        targets.extend(
            _target(item, reason="interruption_late_yield", polarity="negative")
            for item in _decisions_between(
                decisions, post_grace, end, actions=SPEECH_ACTIONS
            )
        )
        if not any(item.action == "interrupt" for item in yield_decisions):
            targets.extend(
                _target(item, reason="interruption_missing_interrupt", polarity="negative")
                for item in _decisions_between(
                    decisions, start, end, actions={"listen"}
                )
            )
    if reentry is not None:
        onset = _nearest_decision(decisions, reentry.start_ms, actions=SPEECH_ACTIONS)
        if onset is not None:
            targets.append(
                _target(onset, reason="interruption_reentry", polarity="positive")
            )
    return {
        "trainable": bool(targets),
        "metrics": {
            "grace_ms": grace_ms,
            "speech_after_grace_ms": overlap_ms,
            "yielded_within_grace": overlap_ms == 0,
            "last_audible_ms": last_audible,
            "yield_latency_ms": yield_latency,
            "reentry_latency_ms": (
                None if reentry is None else reentry.start_ms - end
            ),
        },
        "causal_targets": targets,
    }


def evaluate_trace(
    events: Sequence[dict[str, Any]],
    annotations: Sequence[dict[str, Any]],
    *,
    clock: str = "recv",
    merge_gap_ms: int = 1000,
    short_episode_ms: int = 1000,
    backchannel_tolerance_ms: int = 1000,
    interruption_grace_ms: int = 160,
) -> dict[str, Any]:
    if clock not in {"recv", "source"}:
        raise ValueError("clock must be 'recv' or 'source'")
    decisions = extract_decisions(events, clock=clock)
    spans, incomplete_spans = extract_playback_spans(events, clock=clock)
    episodes = merge_speech_episodes(spans, max_gap_ms=merge_gap_ms)
    scored: list[dict[str, Any]] = []
    for index, ann in enumerate(annotations):
        event_type = str(ann["type"])
        if event_type == "turn":
            result = _score_turn(ann, decisions, episodes)
        elif event_type == "pause":
            result = _score_pause(
                ann, decisions, episodes, short_episode_ms=short_episode_ms
            )
        elif event_type == "backchannel":
            result = _score_backchannel(
                ann,
                decisions,
                episodes,
                short_episode_ms=short_episode_ms,
                tolerance_ms=backchannel_tolerance_ms,
            )
        else:
            result = _score_interruption(
                ann,
                decisions,
                episodes,
                grace_ms=interruption_grace_ms,
            )
        scored.append(
            {
                "id": str(ann.get("id") or f"{event_type}-{index + 1}"),
                "type": event_type,
                **result,
            }
        )

    action_counts = {
        action: sum(1 for item in decisions if item.action == action)
        for action in ACTION_MAP
    }
    trainable = sum(1 for item in scored if item["trainable"])
    readiness_reasons: list[str] = []
    if not decisions:
        readiness_reasons.append("no foreground.decision events")
    if not spans:
        readiness_reasons.append("no completed/cancelled playback spans")
    if not annotations:
        readiness_reasons.append("no annotated conversational events")
    if incomplete_spans:
        readiness_reasons.append(
            f"{incomplete_spans} playback span(s) have no terminal ACK"
        )
    return {
        "method": "gander_event_causal_v1",
        "clock": clock,
        "mapping": {
            action: map_gander_action(action) for action in ACTION_MAP
        },
        "trace": {
            "timeline_events": len(events),
            "decision_events": len(decisions),
            "playback_spans": len(spans),
            "speech_episodes": len(episodes),
            "incomplete_playback_spans": incomplete_spans,
            "action_counts": action_counts,
        },
        "annotations": scored,
        "training_readiness": {
            "ready_for_offline_credit_analysis": (
                bool(decisions) and bool(spans) and bool(annotations) and trainable > 0
            ),
            "trainable_annotations": trainable,
            "total_annotations": len(scored),
            "reasons": readiness_reasons,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Score GNSIS timeline traces with HiPLEX-style event-causal credit."
    )
    parser.add_argument("--timeline", required=True, help="SessionTimeline JSONL")
    parser.add_argument("--annotations", required=True, help="Annotated event JSON")
    parser.add_argument("--out", required=True, help="Output report JSON")
    parser.add_argument(
        "--clock",
        choices=["recv", "source"],
        default="recv",
        help="Use server receive time by default; source falls back to receive time.",
    )
    parser.add_argument("--merge-gap-ms", type=int, default=1000)
    parser.add_argument("--short-episode-ms", type=int, default=1000)
    parser.add_argument("--backchannel-tolerance-ms", type=int, default=1000)
    parser.add_argument("--interruption-grace-ms", type=int, default=160)
    args = parser.parse_args(argv)

    report = evaluate_trace(
        load_timeline(args.timeline),
        load_annotations(args.annotations),
        clock=args.clock,
        merge_gap_ms=args.merge_gap_ms,
        short_episode_ms=args.short_episode_ms,
        backchannel_tolerance_ms=args.backchannel_tolerance_ms,
        interruption_grace_ms=args.interruption_grace_ms,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report["training_readiness"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
