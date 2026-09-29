"""Evaluate grounding and execution from actual GNSIS runs.

Input is the recorder's own JSONL (:mod:`.real_runs`), read with the same
record type that wrote it — there is no conversion step and no second schema.
There is no synthetic episode regeneration and no dependency on old
checkpoints.

When executor-side target geometry is available, the report compares every
recorded candidate variant (raw, raw+r24, OCR, OCR+r24) geometrically. That is
a counterfactual score. Separately, it reports the observed outcome of the
variant that actually executed: actuator success, and verified success from
the post-action frames, with ambiguous verifications counted apart rather than
as failures.

Ported from the browser repository's ``services/visual-engine/scripts/
eval_real_runs.py`` (Gnsis-browser PR #7), which scored a schema the runtime
never wrote. One deliberate difference: a variant that was never probed
(``unavailable``) is not scored at all, where PR #7 counted it as a geometric
miss — a counterfactual nobody computed is neither right nor wrong.

Run:  python -m gnsis_runtime.visual.evaluation --runs real-runs.jsonl [--context browser] [--latest N] [--out report.json]
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from statistics import median
from typing import Any, Iterable

from .real_runs import SCHEMA_VERSION, VARIANTS, RealRunRecord

REPORT_VERSION = 2


def geometric_score(record: RealRunRecord, variant: str) -> bool | None:
    """Whether a candidate lands in executor-recorded target geometry.

    None when the run recorded no evaluation-only geometry or never probed the
    variant. A probed variant that abstained counts as a miss.
    """

    if record.target_box is None:
        return None
    candidate = record.candidates[variant]
    if candidate.status == "unavailable":
        return None
    if candidate.status != "resolved" or candidate.point is None:
        return False
    return record.target_box.contains(candidate.point)


def ratio(ok: int, total: int) -> dict[str, int | float | None]:
    return {"ok": ok, "total": total, "rate": round(ok / total, 4) if total else None}


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(q * (len(ordered) - 1)))
    return round(ordered[index], 2)


def load_records(lines: Iterable[str], *, source: str = "<runs>") -> list[RealRunRecord]:
    records = []
    for line_no, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            records.append(RealRunRecord.from_json(json.loads(line)))
        except Exception as exc:
            raise ValueError(f"{source}:{line_no}: {exc}") from exc
    return records


def evaluate(records: list[RealRunRecord]) -> dict[str, Any]:
    geometry: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    availability: dict[str, int] = defaultdict(int)
    abstentions: dict[str, int] = defaultdict(int)
    actuator: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    verified: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    ambiguous: dict[str, int] = defaultdict(int)
    corrections: dict[str, int] = defaultdict(int)
    latency: dict[str, list[float]] = defaultdict(list)
    recovery = {"attempted": 0, "succeeded": 0, "failed": 0}
    first_attempt: list[int] = [0, 0]
    contexts: dict[str, int] = defaultdict(int)
    actions: dict[str, int] = defaultdict(int)
    statuses: dict[str, int] = defaultdict(int)

    for record in records:
        contexts[record.context] += 1
        actions[record.action] += 1
        statuses[record.verification_status] += 1

        for variant in VARIANTS:
            candidate = record.candidates[variant]
            if candidate.status != "unavailable":
                availability[variant] += 1
            if candidate.status == "abstained":
                abstentions[variant] += 1
            score = geometric_score(record, variant)
            if score is not None:
                for key in ("all", record.action):
                    geometry[variant][key][0] += int(score)
                    geometry[variant][key][1] += 1

        if record.recovery_attempted:
            recovery["attempted"] += 1
            if record.recovery_success is True:
                recovery["succeeded"] += 1
            elif record.recovery_success is False:
                recovery["failed"] += 1
        elif record.verified_success is not None:
            first_attempt[0] += int(record.verified_success)
            first_attempt[1] += 1

        executed = record.execution.executed_variant
        if executed is None:
            continue
        if record.execution.actuator_success is not None:
            actuator[executed][0] += int(record.execution.actuator_success)
            actuator[executed][1] += 1
        if record.verified_success is None:
            ambiguous[executed] += 1
        else:
            verified[executed][0] += int(record.verified_success)
            verified[executed][1] += 1
        if record.user_corrected:
            corrections[executed] += 1
        if record.execution.latency_ms is not None:
            latency[executed].append(record.execution.latency_ms)

    return {
        "report_version": REPORT_VERSION,
        "record_schema_version": SCHEMA_VERSION,
        "cases": len(records),
        "time_range_ms": {
            "first": min((r.captured_at_ms for r in records), default=None),
            "last": max((r.captured_at_ms for r in records), default=None),
        },
        "contexts": dict(sorted(contexts.items())),
        "actions": dict(sorted(actions.items())),
        "verification": dict(sorted(statuses.items())),
        "candidate_coverage": {variant: ratio(availability[variant], len(records)) for variant in VARIANTS},
        "geometry_accuracy": {
            variant: {action: ratio(*values) for action, values in sorted(geometry[variant].items())}
            for variant in VARIANTS
        },
        "abstentions": {variant: abstentions[variant] for variant in VARIANTS},
        "live_execution": {
            variant: {
                "actuator_success": ratio(*actuator[variant]),
                "verified_success": ratio(*verified[variant]),
                "verification_ambiguous": ambiguous[variant],
                "user_corrections": corrections[variant],
                "latency_ms_p50": round(median(latency[variant]), 2) if latency[variant] else None,
                "latency_ms_p95": percentile(latency[variant], 0.95),
            }
            for variant in VARIANTS
        },
        "first_attempt_verified_success": ratio(*first_attempt),
        "recovery": recovery,
    }


def main(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--runs", required=True, help="the recorder's real-runs.jsonl")
    parser.add_argument("--out")
    parser.add_argument("--context", choices=("browser", "desktop"), help="evaluate only one execution context")
    parser.add_argument("--since-ms", type=int, default=0, help="only runs captured at or after this timestamp")
    parser.add_argument("--latest", type=int, default=0, help="evaluate only the newest N matching runs")
    args = parser.parse_args(argv)

    try:
        records = load_records(Path(args.runs).read_text(encoding="utf-8").splitlines(), source=args.runs)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    records = [
        record
        for record in records
        if (not args.context or record.context == args.context) and record.captured_at_ms >= args.since_ms
    ]
    records.sort(key=lambda record: (record.captured_at_ms, record.case_id))
    if args.latest > 0:
        records = records[-args.latest :]

    report = evaluate(records)
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return report


if __name__ == "__main__":
    main()
