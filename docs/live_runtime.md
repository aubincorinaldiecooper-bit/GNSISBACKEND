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

## Validation

```bash
python -m pip install pytest pytest-asyncio fastapi httpx pillow pyyaml websockets "modal==1.5.0"
python -m pip install --no-deps -e ./runtime/minicpm_ft -e ./runtime/gnsis_runtime
pytest -q runtime/gnsis_runtime/tests
```

After deployment, use `scripts/verify-modal-gnsis.py` to resolve the public address. Set `GNSIS_SMOKE=1` to call `/health`; that cold-starts the GPU.

The final MVP check is a real `/live` session: camera permission succeeds, the session reaches `Session ready.`, the model receives frames, and GNSIS answers.
