# The live runtime: the phone page and the model behind it

The live runtime is the part of GNSIS a phone talks to. Scan the QR code on a computer, or tap Start on a phone, allow the camera and the microphone, and point the phone at something: the model watches and listens in real time and answers out loud, in text, and through the phone's haptics.

It is a separate component from the GNSIS service. The service is a FastAPI API and a Celery worker on Railway, with a standard-library core tested on Python 3.9. The live runtime is a GPU program on Modal: MiniCPM-o 4.5 with the GNSIS Thinker checkpoint, Python 3.10+, FastAPI, torch. They share this repository and nothing else; neither imports the other.

## Where things are

| path | what |
| --- | --- |
| `live/runtime/` | The runtime: the duplex websocket (`/ws/duplex`), the screen socket (`/ws/screen`), the phone page (`/live`) and its QR (`/live/qr.svg`), the built-in tools, and the Ornith worker provider. |
| `live/minicpm_ft/` | The MiniCPM-o 4.5 inference core (`mcpmft`) and the page's static files, including the brand assets under `mcpmft/infer/static/live/`. |
| `live/configs/gnsis-live.yaml` | The MVP deployment configuration: model paths, realtime duplex/vision settings, and `worker.provider: none`. |
| `modal/live.py` | The Modal definition: app `gnsis-live`, function `gnsis_live_server`, an L40S GPU, the model volume and the secret. |
| `modal/ornith.py` | The brain the runtime calls for tasks: app `gnsis-ornith`, Ornith-1.5-9B served by vLLM on an L40S. See below. |
| `scripts/verify-modal-live.py` | After a deploy: finds the app and prints its address; with `GNSIS_SMOKE=1` it also opens `/health`. |
| `scripts/verify-modal-ornith.py` | The same for the brain; with `ORNITH_SMOKE=1` it asks what the server serves. |
| `gnsis/UPSTREAM.md` | Provenance: upstream, the time in GNSIS, the move here. |

## Tests

The runtime's tests stub the model, so they run without a GPU, torch or a checkpoint:

```bash
python -m pip install pytest pytest-asyncio fastapi httpx pillow pyyaml websockets "modal==1.5.0"
python -m pip install --no-deps -e ./live/minicpm_ft -e ./live/runtime
pytest -q live/runtime/tests
```

CI runs exactly this in `.github/workflows/live-validation.yml`, only when files under `gnsis/`, the Modal definition or the verify script change. The main `ci.yml` never sees the runtime: it lives outside `src/` and `tests/`, and the Railway image leaves it out through `.dockerignore`.

## Deploying

The production Modal credentials live on **Railway → GNSISWORKER**, not in
GitHub Actions. The worker owns the Modal workspace relationship in the same
shape GNSIS's worker used.

Required on GNSISWORKER:

| kind | name | value |
| --- | --- | --- |
| secret | `MODAL_TOKEN_ID` | Modal workspace token id. |
| secret | `MODAL_TOKEN_SECRET` | Modal workspace token secret. |
| variable | `MODAL_ENVIRONMENT` | Optional; defaults to `main`. |
| variable | `GNSIS_MODELS_VOLUME` | Optional; defaults to `gnsis-models`. |

The worker image includes `gnsis/` and `modal/` and installs `modal==1.5.0`.
Deployment is explicit, not a startup side effect:

```python
from gnsis.service.tasks import deploy_live_runtime
result = deploy_live_runtime.run(smoke=False)
```

To inspect the current deployment without redeploying:

```python
from gnsis.service.tasks import modal_gnsis_status
result = modal_gnsis_status.run(smoke=False)
```

Setting `smoke=True` opens `/health`, which cold-starts the GPU and may take
several minutes. GitHub Actions validates code but does not hold the production
Modal token or deploy the live runtime.

## Ornith is parked for the MVP

The current MVP deliberately runs GNSIS with `worker.provider: none`. Realtime vision and duplex interaction do not depend on Ornith, and the `gnsis-live` Modal app no longer requires the historical Ornith secret. The Ornith provider and deployment definition remain in the repository for a later action-layer phase, but they are not part of MVP testing.

That service was deployed from a notebook and no repository described it, so it could not be rebuilt. `modal/ornith.py` is that description, brought over from an earlier internal revision. Two things follow from how it is written:

- **It publishes under `gnsis-ornith`, not over the running service.** The live one is `legacy-ornith-service`, deployed from the notebook, and the runtime is still pointed at it. A publish from this repository creates or updates the GNSIS-owned app beside it and cannot replace it by accident. Setting `ORNITH_MODAL_APP_NAME` to the old name adopts it in place instead; the function keeps the name that deployment uses, so the address callers hold does not change.
- **The cache volume and the secret must already exist.** `gnsis-ornith-cache` and `gnsis-ornith-auth` by default, overridable with `ORNITH_CACHE_VOLUME` and `GNSIS_SECRET_NAME`. A name that does not exist fails the publish rather than creating an empty cache that re-downloads the weights on every cold start.

Publishing works like the runtime's own, from the worker that holds the token:

```python
from gnsis.service.tasks import deploy_ornith_brain, ornith_status
deploy_ornith_brain.run()   # publishes, then returns the address
ornith_status.run()         # just the address, nothing started
```

Or through the internal API, which only enqueues onto the worker:

```sh
POST /internal/compute/ornith/deploy   {"confirm":"gnsis-ornith"}
POST /internal/compute/ornith/status
```

If the action layer is re-enabled later, add an explicit worker configuration and point it at the verified Ornith deployment. Do not make that cutover part of the current MVP.

The vLLM image is `vllm/vllm-openai:latest`, mirroring the notebook for parity. `latest` moves, and a vLLM release that renames a serving flag would break a publish that changed nothing else. Pin the resolved digest in `ORNITH_VLLM_IMAGE` once parity with the running service is proven.

## Cutover from `legacy-live-service`

The same runtime ran from GNSIS as the Modal app `legacy-live-service`. The new app uses the same volume and the same secret, so the two can run side by side.

1. Deploy `gnsis-live` (above). Keep `legacy-live-service` running.
2. Verify the new app. `GNSIS_SMOKE=1 python scripts/verify-modal-live.py` opens `/health`, checks the status and the tool list, and prints the settings the runtime reports about itself; that starts a GPU container, and a cold start takes several minutes. Then open the printed address with `/live` on a computer and scan the QR with a phone. A session that reaches *Session ready.* is the real check.
3. Compare the settings with the app being replaced: `GNSIS_HEALTH_URL=https://<the old app's address> python scripts/verify-modal-live.py` prints the same lines for it, with no Modal token. Each line is named by the key that sets it in `live/configs/gnsis-live.yaml`, because the file is where a difference has to go and its loader refuses any key it does not know; `/health` itself calls the slate setting `task_slate_visible_to_model`, while the file's key is `duplex.expose_task_slate_to_model`, and its `client_video` and `asr_enabled` fields are derived from `duplex.allow_client_video`, `client_video_mode`, `client_video_sources` and `asr.mode`. an earlier internal revision said the live app's duplex settings differed from the checked-in configuration in nine values, of which the comparison shows four: `sliding_window_mode`, `context_max_units`, `context_previous_max_tokens` and `expose_task_slate_to_model`. If the old app reports different values, put them into the file under those keys and redeploy before switching. The other five (`trailing_silence_sec`, `turn_bind_grace_sec`, `speak_text_tokens_per_unit`, `max_new_speak_tokens_per_chunk`, and the two tool token limits) are only visible in the old app's own configuration.
4. Point whatever used the old address at the new one.
5. Stop the old app: `modal app stop legacy-live-service` in the same environment.

The model artifacts on the existing volume are not touched by any of this.

## What the move did not verify

The move was checked by the runtime's test suite and by rendering the page from the real app with the model stubbed. No deployment was made from this repository and no phone was used. The first `gnsis-live` deploy, the `/health` smoke and a live session on a phone are the cutover's own steps above.

`modal/ornith.py` has never been published. It remains future action-layer infrastructure only; the MVP runtime does not call it.
