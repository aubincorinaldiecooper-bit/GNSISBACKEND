# Visual System-1 migration manifest

This manifest prevents migration-by-assumption. Nothing in the source visual engine is
considered disposable merely because the backend has an adjacent concept.

## Meaning of "lightweight"

"Lightweight" applies only to the default dependency/import surface of
`gnsis-runtime`. It does **not** mean "move only the easy files", "drop heavy
capabilities", or "omit tests/training/evaluation code".

The migration is complete only when every tracked file under
`Gnsis-browser/services/visual-engine/` is explicitly classified and either:

1. migrated into GNSISBACKEND;
2. retained as a browser-environment adapter;
3. replaced by an existing backend implementation with parity proven; or
4. removed only after its behavior is covered elsewhere and tested.

Until then, the browser source remains the reference implementation.

## File-by-file ownership map

| Source | Ownership | Migration disposition |
| --- | --- | --- |
| `gnsis_visual/backbone.py` | GNSIS core | **MOVE** — migrated in PR #120 |
| `gnsis_visual/batching.py` | GNSIS core | **MOVE** — migrated in PR #120 |
| `gnsis_visual/decode.py` | GNSIS core | **MOVE** — migrated in PR #120 |
| `gnsis_visual/engine.py` | GNSIS core | **MOVE/ADAPT** — model/cache logic migrated; persistent-frame adapter added |
| `gnsis_visual/head.py` | GNSIS core | **MOVE** — migrated in PR #120 |
| `gnsis_visual/labels.py` | training/eval | **MOVE** — migrated in PR #120 |
| `gnsis_visual/ocr.py` | GNSIS grounding | **MOVE** — migrated in PR #120 |
| `gnsis_visual/prompt.py` | GNSIS core | **MOVE** — migrated in PR #120 |
| `gnsis_visual/schema.py` | shared contract | **MOVE/ADAPT** — migrated; backend string frame IDs supported |
| `gnsis_visual/actuator.py` | browser adapter | **STAY** — Playwright coordinate execution |
| `gnsis_visual/stream.py` | browser adapter | **STAY** — CDP screencast acquisition; backend already owns its persistent screen-frame path |
| `gnsis_visual/session.py` | mixed | **SPLIT** — Page/context/actuator lifecycle stays browser-side; goal/history/cache/decision-session semantics must be represented in backend before source deletion |
| `gnsis_visual/server.py` | mixed / transitional API | **REPLACE, DO NOT COPY BLINDLY** — browser/tab lifecycle stays adapter-side; model-independent decide/step/run semantics must map onto backend control surfaces |
| `gnsis_visual/mcp_server.py` | control surface | **REPLACE WITH BACKEND MCP** — preserve tool semantics intentionally; do not add a second MCP server |
| `gnsis_visual/tasks/episodes.py` | training/eval | **MOVE** |
| `gnsis_visual/tasks/harness.py` | training/eval browser harness | **MOVE/ADAPT** — oracle remains evaluation-only |
| `gnsis_visual/tasks/runtime.js` | training/eval fixture | **MOVE** |
| `gnsis_visual/tasks/sites.py` | training/eval fixture | **MOVE** |
| `gnsis_visual/tasks/vocab.py` | training/eval | **MOVE** |
| `scripts/bench_backbone.py` | benchmark | **MOVE** |
| `scripts/collect.py` | data collection | **MOVE/ADAPT** — continues to use browser adapter for rendered-stream collection |
| `scripts/eval_e2e.py` | evaluation | **MOVE/ADAPT** — backend policy + browser adapter |
| `scripts/eval_snap.py` | grounding evaluation | **MOVE** |
| `scripts/extract.py` | feature extraction | **MOVE** |
| `scripts/modal_extract.py` | training/inference infra | **MOVE/ADAPT** — preserve `gnsis-visual-data` volume contract and artifact paths |
| `scripts/train_head.py` | training | **MOVE** |
| `tests/test_contract.py` | core regression | **MOVE** — preserve both contract and learned-head tests |
| `tests/test_api.py` | transitional API regression | **ADAPT** — retain equivalent behavior tests across backend↔browser seam |
| `tests/test_mcp.py` | MCP integration | **ADAPT** — preserve end-to-end semantics against backend MCP rather than copying standalone server |
| `tests/conftest.py` | test plumbing | **ADAPT AS NEEDED** |
| `pyproject.toml` | package metadata | **MERGE** into backend packaging rather than copy |
| `requirements.txt` | dependency inventory | **MERGE COMPLETELY** into explicit backend extras/dev tooling; no dependency silently dropped |
| `.gitignore` | repo plumbing | **RECONCILE** |
| `AGENTS.md` | architecture/status record | **PRESERVE/MERGE** into backend visual documentation before browser copy is retired |

## Behaviors that must survive

- frozen MiniCPM-V visual feature extraction;
- JEV action/value/target pointer behavior;
- sub-cell target offset decoding;
- goal-derived value candidates;
- bounded structured action history;
- visual-cache reuse on unchanged frames;
- motion signal conditioning;
- pixel-only RapidOCR target refinement;
- exact decision validation rules;
- generated train/test vocabulary split;
- synthetic episode variants including overlays/loading/error states;
- oracle isolation: ground truth is scoring/training only, never perception input;
- collection from a persistent rendered stream;
- local and Modal feature extraction;
- sharded feature extraction;
- 30-epoch head training path;
- backbone benchmark path;
- offline OCR benchmark;
- browser-backed end-to-end evaluation;
- HTTP/WebSocket/MCP behavior where those semantics remain useful;
- existing benchmark/artifact compatibility.

## Deletion gate

Do **not** delete `services/visual-engine` from the browser repository until:

1. every row above has a resolved destination;
2. training/eval scripts run from their backend-owned location;
3. browser adapter parity is tested;
4. backend MCP/control semantics cover the useful old API semantics;
5. OCR+r24 can be evaluated across the real backend↔browser boundary;
6. existing model checkpoints/features remain load-compatible or a deliberate migration is documented;
7. no regression test was dropped merely because its dependency is optional.

PR #120 is therefore a **core migration slice**, not a declaration that the full
visual-engine migration is complete.

## Laya retirement (single System-1 path)

The backend JEV/MiniCPM-V path ("Smaller GNSIS") is the only System-1 decision
engine going forward. What it already had, what was added to subsume the
browser fork's Panoptic -> Laya contract, what stays unique to Laya, and the
measurable gate that must pass before Laya is deleted from `Gnsis-browser` are
recorded in [`laya-retirement.md`](laya-retirement.md).
