# HiPLEX-style event-causal evaluation for Gander

## Decision

Do not replace Gander's learned control vocabulary with HiPLEX's three-way
factorization.

Gander already exposes a more useful interaction policy:

| Gander action | HiPLEX-compatible role | Keep distinct? |
| --- | --- | --- |
| `listen` | pad-like / hold the floor | yes |
| `speak` | cont-like sustained speech | yes |
| `backchannel` | cont-like short acknowledgement | yes |
| `interrupt` | explicit yield action | yes |
| `tool` | excluded from conversational timing reward | yes |

The reusable idea is **event-causal credit assignment**: timing feedback should
touch only decisions that could have caused the observed turn-taking event,
rather than every token in the response.

## What landed

Live Gander units now preserve a normalized `decision` value and the
coordinator writes it to the existing shared timeline as
`foreground.decision`.

The timeline already receives real Host playback facts
(`playback.started`, `playback.completed`, `playback.cancelled`), so the
offline scorer evaluates what the person actually heard rather than what the
Talker merely generated.

No second trace system is introduced.

## Offline scorer

Run:

```bash
mcpmft-hiplex-eval \
  --timeline /path/to/session.timeline.jsonl \
  --annotations /path/to/events.json \
  --out /tmp/hiplex-report.json
```

Annotation file:

```json
{
  "events": [
    {
      "id": "turn-1",
      "type": "turn",
      "start_ms": 1000,
      "end_ms": 2400,
      "anchor_ms": 2400
    },
    {
      "id": "pause-1",
      "type": "pause",
      "start_ms": 6000,
      "end_ms": 7200
    },
    {
      "id": "bc-1",
      "type": "backchannel",
      "start_ms": 9000,
      "end_ms": 9800,
      "opportunity_ms": 9400
    },
    {
      "id": "interrupt-1",
      "type": "interruption",
      "start_ms": 12000,
      "end_ms": 13200
    }
  ]
}
```

The scorer reconstructs audible speech episodes from playback ACKs, merges
small gaps, and reports:

- turn response onset and early speech;
- pause intrusion;
- backchannel match / miss / overlong behavior;
- interruption speech after the grace window, yield latency and re-entry;
- the exact Gander decisions eligible for positive or negative timing credit.

## Important granularity rule

HiPLEX's published model operates at substantially finer temporal steps than
Gander's current one-second interaction units. Do not pretend the training
grids are equivalent.

The offline analysis uses millisecond timestamps from the GNSIS timeline and
actual playback facts. A later training implementation may assign the resulting
event credit to Gander units, but it must not invent sub-unit decisions the
model never made.

## Gate before training

Do not add GRPO/PPO code yet.

First collect real traces for turn, pause, backchannel and interruption cases
and require:

1. `foreground.decision` is present for the model units;
2. playback spans terminate cleanly;
3. annotations produce non-empty event-causal targets;
4. the scorer's measured interruption/yield result agrees with what a human
   hears in the trace;
5. backchannels are preserved as `backchannel`, not collapsed to `speak`.

Only after that evidence exists should the event-causal targets be wired into
a policy-optimization loss.
