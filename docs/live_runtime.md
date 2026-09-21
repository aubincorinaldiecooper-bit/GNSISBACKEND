# GNSIS live runtime

The live runtime is GNSIS's realtime perception surface. A person opens `/live`,
grants camera and microphone access, and interacts with the model while it sees
and listens continuously. The same runtime also accepts screen frames through
`/ws/screen`.

The production GPU runtime is separate from the Railway API/worker processes but
lives in this repository and is deployed by GNSIS.

## Layout

| path | purpose |
| --- | --- |
| `live/runtime/` | Realtime WebSocket runtime, session lifecycle, native tools, QR surface and optional worker-provider interface. |
| `live/minicpm_ft/` | MiniCPM-o inference/fine-tuning core and the static `/live` assets. |
| `live/configs/gnsis-live.yaml` | Production live configuration. |
| `modal/live.py` | Modal app definition for `gnsis-live`. |
| `scripts/verify-modal-live.py` | Deployment discovery and optional `/health` smoke verification. |
| `live/PROVENANCE.md` | Upstream provenance for the vendored runtime. |

## MVP configuration

The current MVP is perception-only:

- MiniCPM-o 4.5 + the GNSIS Thinker checkpoint
- realtime camera and screen vision
- duplex interaction
- `/live` QR/phone experience
- semantic haptics
- `worker.provider: none`

The live session therefore has no external action-layer dependency.

## Model storage

The Modal app mounts one GNSIS-owned volume:

`gnsis-models`

The expected paths are:

```text
/models/MiniCPM-o-4_5
/models/GNSIS/thinker
```

Override the volume name with `GNSIS_MODELS_VOLUME` when required.

## Tests

The runtime suite stubs model loading, so it can run without a GPU:

```bash
python -m pip install pytest pytest-asyncio fastapi httpx pillow pyyaml websockets "modal==1.5.0"
python -m pip install --no-deps -e ./live/minicpm_ft -e ./live/runtime
pytest -q live/runtime/tests
```

The dedicated workflow is `.github/workflows/live-validation.yml`.

## Deployment

Production Modal credentials belong only on `GNSISWORKER`:

| variable | requirement |
| --- | --- |
| `MODAL_TOKEN_ID` | required |
| `MODAL_TOKEN_SECRET` | required |
| `MODAL_ENVIRONMENT` | optional; defaults to `main` |
| `GNSIS_MODELS_VOLUME` | optional; defaults to `gnsis-models` |

Deployment is explicit:

```python
from gnsis.service.tasks import deploy_live_runtime
result = deploy_live_runtime.run(smoke=False)
```

Status without a GPU cold start:

```python
from gnsis.service.tasks import modal_gnsis_status
result = modal_gnsis_status.run(smoke=False)
```

With `smoke=True`, GNSIS opens `/health`, which starts the GPU container and
may take several minutes on a cold deployment.

## MVP device test

After deployment:

1. Open the reported `gnsis-live` address with `/live`.
2. Scan the QR from a phone or open the page directly on the phone.
3. Grant camera and microphone access.
4. Confirm the page reaches `Session ready.`.
5. Point the camera at an object and ask a visual question.
6. Confirm the answer reflects the live view.
7. Repeat with a changed scene to confirm temporal updates are reaching the model.

This is the release gate for the realtime MVP.

## Optional action layer

Ornith remains an optional worker provider for future delegated tasks. Its
deployment definition is `modal/ornith.py`, with GNSIS-owned defaults:

- app: `gnsis-ornith`
- cache volume: `gnsis-ornith-cache`
- secret: `gnsis-ornith-auth`

The related environment variables are `ORNITH_MODAL_APP_NAME`,
`ORNITH_CACHE_VOLUME`, and `ORNITH_SECRET_NAME`.

None of these are required by the live perception MVP.
