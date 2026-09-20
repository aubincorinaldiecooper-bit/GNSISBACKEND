# The live runtime: the phone page and the model behind it

The live runtime is the part of GNSIS a phone talks to. Scan the QR code on a computer, or tap Start on a phone, allow the camera and the microphone, and point the phone at something: the model watches and listens in real time and answers out loud, in text, and through the phone's haptics.

It is a separate component from the GNSIS service. The service is a FastAPI API and a Celery worker on Railway, with a standard-library core tested on Python 3.9. The live runtime is a GPU program on Modal: MiniCPM-o 4.5 with the Gander Thinker checkpoint, Python 3.10+, FastAPI, torch. They share this repository and nothing else; neither imports the other.

## Where things are

| path | what |
| --- | --- |
| `gander/gander_runtime/` | The runtime: the duplex websocket (`/ws/duplex`), the screen socket (`/ws/screen`), the phone page (`/live`) and its QR (`/live/qr.svg`), the built-in tools, and the Ornith worker provider. |
| `gander/minicpm_ft/` | The MiniCPM-o 4.5 inference core (`mcpmft`) and the page's static files, including the brand assets under `mcpmft/infer/static/live/`. |
| `gander/configs/gnsis-live.yaml` | The deployment configuration: model paths on the volume, duplex settings, the Ornith worker. |
| `modal/gander.py` | The Modal definition: app `gnsis-live`, function `gander_server`, an L40S GPU, the model volume and the secret. |
| `scripts/verify-modal-gander.py` | After a deploy: finds the app and prints its address; with `GANDER_SMOKE=1` it also opens `/health`. |
| `gander/UPSTREAM.md` | Provenance: upstream, the time in CLIPIT, the move here. |

## Tests

The runtime's tests stub the model, so they run without a GPU, torch or a checkpoint:

```bash
python -m pip install pytest pytest-asyncio fastapi httpx pillow pyyaml websockets "modal==1.5.0"
python -m pip install --no-deps -e ./gander/minicpm_ft -e ./gander/gander_runtime
pytest -q gander/gander_runtime/tests
```

CI runs exactly this in `.github/workflows/gander-validation.yml`, only when files under `gander/`, the Modal definition or the verify script change. The main `ci.yml` never sees the runtime: it lives outside `src/` and `tests/`, and the Railway image leaves it out through `.dockerignore`.

## Deploying

`.github/workflows/deploy-gander.yml` deploys on every push to `main` that touches the runtime, and on demand from the Actions tab. It needs, on this repository:

| kind | name | value |
| --- | --- | --- |
| secret | `MODAL_TOKEN_ID` | A Modal token for the workspace that owns the model volume. |
| secret | `MODAL_TOKEN_SECRET` | Its secret. |
| variable | `GANDER_MODELS_VOLUME` | Optional. The existing Modal Volume holding `MiniCPM-o-4_5/` and `Gander/thinker/`. Defaults to `clipit-gander-weights`, the name CLIPIT #134 recorded from the live deployment. |
| variable | `GANDER_SECRET_NAME` | Optional. The existing Modal Secret holding `ORNITH_API_KEY`. Defaults to `clipit-gander-ornith`, from the same place. |
| variable | `MODAL_ENVIRONMENT` | Optional. Defaults to `main`. |

A deploy creates nothing: the volume and the secret must already exist, and a name that does not exist fails the deploy, so a wrong name can never create a second production resource by accident. The two token secrets are the only values a deploy cannot do without.

By hand, from the repository root, with the two token values in the environment (and the names, if they differ from the defaults):

```bash
python -m pip install "modal==1.5.0"
modal deploy -e main modal/gander.py
python scripts/verify-modal-gander.py
```

## Cutover from `clipit-gander-thinker`

The same runtime ran from CLIPIT as the Modal app `clipit-gander-thinker`. The new app uses the same volume and the same secret, so the two can run side by side.

1. Deploy `gnsis-live` (above). Keep `clipit-gander-thinker` running.
2. Verify the new app. `GANDER_SMOKE=1 python scripts/verify-modal-gander.py` opens `/health`, checks the status and the tool list, and prints the settings the runtime reports about itself; that starts a GPU container, and a cold start takes several minutes. Then open the printed address with `/live` on a computer and scan the QR with a phone. A session that reaches *Session ready.* is the real check.
3. Compare the settings with the app being replaced: `GANDER_HEALTH_URL=https://<the old app's address> python scripts/verify-modal-gander.py` prints the same lines for it, with no Modal token. Each line is named by the key that sets it in `gander/configs/gnsis-live.yaml`, because the file is where a difference has to go and its loader refuses any key it does not know; `/health` itself calls the slate setting `task_slate_visible_to_model`, while the file's key is `duplex.expose_task_slate_to_model`, and its `client_video` and `asr_enabled` fields are derived from `duplex.allow_client_video`, `client_video_mode`, `client_video_sources` and `asr.mode`. CLIPIT #134 said the live app's duplex settings differed from the checked-in configuration in nine values, of which the comparison shows four: `sliding_window_mode`, `context_max_units`, `context_previous_max_tokens` and `expose_task_slate_to_model`. If the old app reports different values, put them into the file under those keys and redeploy before switching. The other five (`trailing_silence_sec`, `turn_bind_grace_sec`, `speak_text_tokens_per_unit`, `max_new_speak_tokens_per_chunk`, and the two tool token limits) are only visible in the old app's own configuration.
4. Point whatever used the old address at the new one.
5. Stop the old app: `modal app stop clipit-gander-thinker` in the same environment.

The model artifacts on the volume and the secret are not touched by any of this.

## What the move did not verify

The move was checked by the runtime's test suite and by rendering the page from the real app with the model stubbed. No deployment was made from this repository and no phone was used. The first `gnsis-live` deploy, the `/health` smoke and a live session on a phone are the cutover's own steps above.
