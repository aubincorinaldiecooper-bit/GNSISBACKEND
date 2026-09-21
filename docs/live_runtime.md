# GNSIS realtime runtime

The realtime runtime is the part of GNSIS a phone or browser talks to. Open `/live`, allow camera and microphone access, and GNSIS watches and listens in real time while returning text and haptic feedback.

It is deployed as a GPU service on Modal and lives in this repository under `runtime/`.

## Layout

| path | purpose |
| --- | --- |
| `runtime/gnsis_runtime/` | Realtime server, duplex websocket, screen/camera transport, native tools, and tests. |
| `runtime/minicpm_ft/` | MiniCPM-o inference core plus the live web surface and brand assets. |
| `runtime/configs/gnsis-live.yaml` | Production realtime configuration. |
| `modal/gnsis.py` | Modal definition for app `gnsis-live` and function `gnsis_server`. |
| `scripts/verify-modal-gnsis.py` | Deployment/address verification and optional health smoke test. |
| `runtime/PROVENANCE.md` | Runtime source and licensing provenance. |

## MVP contract

The MVP is perception-first and intentionally has no external action worker:

```yaml
worker:
  provider: none
```

The live configuration enables realtime vision for both camera and screen input. Ornith remains optional future action-layer infrastructure and is not required for the MVP session.

## Modal resources

The realtime deployment expects the GNSIS-owned model volume `gnsis-model-weights`, mounted at `/models`.

The production configuration expects:

```text
/models/MiniCPM-o-4_5
/models/GNSIS/thinker
```

The Railway worker owns the Modal workspace credentials. GitHub Actions performs validation only.

Required worker configuration:

| variable | purpose |
| --- | --- |
| `MODAL_TOKEN_ID` | Modal workspace token id. |
| `MODAL_TOKEN_SECRET` | Modal workspace token secret. |
| `MODAL_ENVIRONMENT` | Optional Modal environment; defaults to `main`. |
| `GNSIS_MODELS_VOLUME` | Optional model-volume override; defaults to `gnsis-model-weights`. |
| `GNSIS_MODAL_APP_NAME` | Optional app override; defaults to `gnsis-live`. |
| `GNSIS_MODAL_FUNCTION_NAME` | Optional function override; defaults to `gnsis_server`. |

## Migrating an existing deployment to this layout

A deployment that predates this naming has the checkpoint under a different
directory inside the volume, and its volume carries a different name. Both are
addressable without copying the weights.

**The volume name does not need to change.** It is an override, so point the
deploy at whatever volume already holds the weights by setting
`GNSIS_MODELS_VOLUME` on the Railway worker to that name. Renaming the volume
itself means creating a new one and copying tens of gigabytes; it buys nothing
the override does not, and can be done later or never.

**The checkpoint directory does need to move,** because the runtime loads it by
path. That is a rename inside one volume, not a copy, so it takes seconds:

```bash
GNSIS_MODELS_VOLUME=<the existing volume> \
GNSIS_THINKER_SOURCE=/models/<previous directory>/thinker \
modal run scripts/migrate-thinker-checkpoint.py
```

It refuses to run without both values, never deletes anything, never writes
over an existing destination, and can be run twice safely. `MiniCPM-o-4_5` is
unaffected; its path is unchanged.

Order matters. Do the rename and set the variable **before** the first
`deploy_live_runtime` from this branch. Merging is safe on its own: nothing
deploys the runtime automatically, and `cache_gnsis_models` fails loudly with
the missing path if a deploy runs against a volume that has not been migrated.

## Validation

```bash
python -m pip install pytest pytest-asyncio fastapi httpx pillow pyyaml websockets "modal==1.5.0"
python -m pip install --no-deps -e ./runtime/minicpm_ft -e ./runtime/gnsis_runtime
pytest -q runtime/gnsis_runtime/tests
```

After deployment, use `scripts/verify-modal-gnsis.py` to resolve the public address. Set `GNSIS_SMOKE=1` to call `/health`; that cold-starts the GPU.

The final MVP check is a real `/live` session: camera permission succeeds, the session reaches `Session ready.`, the model receives frames, and GNSIS answers.
