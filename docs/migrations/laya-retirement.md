# Smaller GNSIS subsumes Laya — audit, additions, retirement gate

"Smaller GNSIS" is the backend-owned System-1 path: the locally trained JEV head
over frozen MiniCPM-V visual features (`gnsis_runtime/visual/engine.py`),
consuming the one persistent live screen stream through
`PersistentVisualDecisionSession`. The browser fork's equivalent is
`Panoptic temporal perception -> Laya bounded decision -> Actuator`
(`Gnsis-browser/AGENTS.md`, `packages/extension/src/vision/LayaClient.ts`,
`packages/extension/src/agent/PanopticPageAgent.ts`, `services/panoptic`).
This document records the comparison and the gate for retiring Laya. Laya is
**not** removed by the change that introduced this document.

## 1. What already existed on backend `main` (not re-implemented)

| Strength | Where |
| --- | --- |
| Structured decision contract; rejects unknown actions, confidence outside [0,1], missing/extra targets, off-viewport targets, `type` without text, non-http(s) `navigate`, bad scroll direction | `visual/schema.py` `validate_decision` |
| Goal-derived value candidates (quoted text, explicit URLs, scroll directions); the head points over them and masks actions whose value kind is absent | `visual/prompt.py` `value_candidates`, `visual/decode.py` |
| One persistent live stream, bounded timestamp-indexed retained frames, no screenshot path | `screen.py` `LatestScreenFrameBuffer`, `docs/persistent-visual-sense/AGENTS.md` |
| Motion over retained frames (800 ms window) as a model input | `visual/runtime.py` `recent_motion` |
| Bounded structured action history | `visual/runtime.py` `record_attempt` |
| Decision bound to a frame id; staleness check | `visual/runtime.py` `is_current` |
| Visual-token cache reuse on unchanged frames | `visual/engine.py` `VisualCache` |
| Execution separated from decision: `StepExecutor` seam, fail-closed `ActionAuthority`, verification-driven control (one alternate attempt, bounded re-looks, escalation), unknown-outcome never retried | `visual/control.py` |
| Semantic post-action verification from the stream; actuator report alone is never success | `visual/verification.py` |
| Real-run records and evaluator (geometry, abstention count, actuator vs verified success, recovery, latency) | `visual/real_runs.py`, `visual/evaluation.py` |

## 2. What was genuinely missing and is now added

| Laya/Panoptic strength | Backend gap on `main` | Added |
| --- | --- | --- |
| Code-generated bounded legal choices (`actionCandidates`, `byKey`) | value candidates existed only inside the model layout; nothing outside the model checked that a decision *was* one of them | `visual/legal.py` `legal_actions` → `LegalActionSet` (generated from goal + observed frame only, via the same `value_candidates`) |
| Strict rejection of invented choices (`!built.byKey.has(choice)` → throw) | `validate_decision` checked structure, not provenance: any `type` text, any http(s) URL, extra arguments (e.g. `click` with text) and decisions bound to another frame passed | `LegalActionSet.match`: text/URL/direction must equal a goal-derived value, no extra arguments, targeted actions must be bound to the observed frame, unknown commands rejected. `PersistentVisualDecisionSession.decide()` raises `IllegalDecision`; nothing is silently absorbed |
| Confidence abstention (`confidence < minConfidence` → `WAIT`, default 0.5) | confidence was computed but never acted on | `DecisionGate.min_confidence` (default 0.5) in `gate_decision` |
| Panoptic temporal state (`silence`/`standby` → keep observing; `change` field) | motion was a model input only; a target read from a still-changing screen could be executed; no notion of "did my last action change anything" | `DecisionGate.unsettled_motion` (targeted proposals wait while motion ≥ 0.35); change since the last attempted action measured against that action's own frame; repeating the same action/arguments/target (≤ 24 px) on an unchanged screen abstains. Exposed as `GatedDecision.change_since_last_action` and in `state()` |
| Deterministic actuator separation (`VisualActuator.execute` only on typed actions) | no single mapping from a System-1 decision to a `VisualStep` | `control.step_from_decision`: reads only `GatedDecision.decision`; abstained/rejected proposals can reach the actuator only as a bounded 500 ms WAIT |
| Fair comparison | no harness compared the two System-1 contracts on identical inputs | `visual/benchmark.py` (below) |

`PersistentVisualDecisionSession.decide_gated()` composes these. The session
still owns no capture source, socket, tab or actuator.

## 3. The common benchmark (`gnsis_runtime/visual/benchmark.py`)

* **Identical inputs.** Each case is an `Observation` (goal, ordered retained
  frames, structured history, stream motion). The `LegalActionSet` is built from
  it by code and handed unchanged to every contestant; every proposal goes
  through the same `gate_decision` as production.
* **No oracle at inference.** Labels are split into `Oracle` at load time and
  read only by scoring (`matches`, `grounded`). Contestants receive an
  `Observation` and a `LegalActionSet`, nothing else.
* **Contestants.** `smaller-gnsis` = `PolicyContestant(JEVEngine)` via the same
  `VisualDecisionPolicy` seam the session uses. `laya-contract` =
  `LayaContractContestant(PanopticStreamPerceiver, LayaHttpChooser)`: one
  Panoptic session per observation fed the ordered frames and history events,
  then Laya's `/v1/systemone` choice over `laya_options` — a faithful port of
  `actionCandidates` (per perceived target click/type, then target-free
  options, `wait`, `done`, capped at 20) projected onto the common legal set.
  A key outside the offered map is an invalid action, as in `LayaClient`.
* **Metrics** (per contestant, plus paired differences on the same cases):
  validity, executed-invalid (must be 0), executed accuracy, proposal accuracy,
  grounding (target inside oracle box), abstention (should-wait recall, false
  abstention), calibration (ECE over 10 bins, Brier, risk–coverage at
  τ ∈ {0, .3, .5, .7, .9}), latency p50/p95, per-action and per-family accuracy.
* **End-to-end.** `run_episode` runs a contestant *inside*
  `PersistentVisualDecisionSession` against an `EpisodeEnvironment` that
  publishes its one persistent stream into a `LatestScreenFrameBuffer` and
  executes `VisualStep`s (the existing `StepExecutor` seam). Success is read
  from the environment only after the episode. Reports task success, false
  `done`, steps, rejections, abstentions. A browser-backed environment adapter
  stays browser-side (see `scripts/eval_e2e.py`).
* **Data.** `load_cases` reads native rows (`frames` + `oracle`) and the
  browser's `scripts/collect.py` `states.jsonl` rows directly.

```
python -m gnsis_runtime.visual.benchmark --cases states.jsonl \
  --contestant smaller-gnsis --model $MINICPM_DIR --head jev_head.pt --device cuda --dtype bfloat16 \
  --contestant laya-contract --laya-url http://127.0.0.1:8791 \
  --panoptic-url ws://127.0.0.1:8792/v1/panoptic/stream \
  --episodes-report episodes.json --out report.json
```

## 4. Laya retirement gate (`benchmark.retirement_gate`)

Laya may be deleted from `Gnsis-browser` only when **every** check passes on a
held-out set (test vocabulary split, never used for head training) with both
contestants run on the same cases at the default gate (`min_confidence` 0.5).
A missing measurement fails its check.

| Check | Requirement |
| --- | --- |
| `paired_cases` | ≥ 500 identical cases scored for both |
| `validity` | Smaller GNSIS proposal validity ≥ 99.5% |
| `executed_invalid` | exactly 0 illegal decisions reach the actuator |
| `accuracy_vs_laya_lower_95` | one-sided 95% lower bound of paired (Smaller GNSIS − Laya) executed accuracy ≥ −0.02 |
| `grounding_vs_laya_lower_95` | same, for grounding on targeted cases, ≥ −0.02 |
| `ece` | Smaller GNSIS ECE ≤ 0.10 |
| `should_wait_recall` | ≥ 0.90 on oracle-WAIT cases (loading, overlays settling) |
| `false_abstention` | ≤ 0.10 on oracle-act cases |
| `latency_p95_ms_vs_laya` | Smaller GNSIS p95 decision latency ≤ Laya's (Panoptic + Laya) p95 |
| `paired_episodes` | ≥ 100 identical end-to-end episodes run for both |
| `task_success_vs_laya_lower_95` | one-sided 95% lower bound of paired task-success difference ≥ −0.02 |
| `false_done` | 0 episodes where Smaller GNSIS claimed `done` without success |

Plus one non-numeric condition: each capability in §5 is either covered by the
backend with a passing case family in the benchmark, or explicitly dropped by
the owner in writing.

## 5. Capabilities that remain unique to Laya (not covered by this change)

* **`SELECT` option choice** (`select:<target>:<i>`). `VisualStep.option` exists,
  but the JEV head has no select action.
* **Horizontal scroll** (`SCROLL_HORIZONTAL`) and **scroll amount**
  (`small` vs `page`). The backend has vertical up/down only.
* **Tab actions** (`SWITCH_TAB`, `CLOSE_TAB`) chosen from the live tab list.
  `VisualStep.tab_id` exists; the JEV head has no tab vocabulary.
* **Unquoted typing values** (`type|enter|input|search for X into`). The backend
  derives text values from quoted strings only.
* **Web-search fallback** (`open:web-search` builds a search URL from the task).
  Deliberately not adopted: the backend opens only addresses the user stated.
* **Semantic perceived targets** (Panoptic's labelled, role/affordance-tagged
  targets and natural-language `summary`/`change`). Smaller GNSIS grounds
  pixel targets directly and has no textual scene description.
* **Long-context temporal memory** (32 rounds / 4096 frames in one Panoptic
  session, standby → high-resolution escalation). The backend uses bounded
  retained frames and an 800 ms motion window.

The common benchmark does not score these: both contestants are restricted to
the shared legal vocabulary, so the Laya contract is offered none of them there
either. They are covered only by the non-numeric gate condition in §4.
