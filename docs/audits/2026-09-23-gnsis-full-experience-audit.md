# GNSIS full-experience audit — 2026-09-23

**Audited:** `main` @ `fbe93156aa0ff0ce012b115ce158deb0b16da757` (the #77 merge). Confirmed unchanged at audit time; no delta to report.
**Production evidence used:** GNSISWORKER deployment `0e647fe5` (commit `fbe9315`, live 21:58 UTC) ran `deploy_live_runtime` task `e6c066f4` at 22:11:52→22:12:48 UTC; its smoke health is quoted in §9. The 22:23–22:29 UTC phone session (`duplex_2d7ee3ef176cd277`, container `ta-01M35KFAKE2JNTMA1FMHPTW04R`) therefore ran this commit — unless the app was also deployed from outside the worker, which Railway's logs cannot show. The session facts themselves (frame counts, RMS values, close code) are the brief's evidence; I have not seen the Modal logs they come from.
**What I could not inspect:** the Modal model volume, Modal container logs, and Modal account entitlements. Every claim that depends on them is marked **unverified** with the exact command that settles it.
**Nothing was deployed, merged, mutated or changed** during this audit. This file is the only artifact.

Confidence tags: **proven** (read in code/config/logs), **strongly supported** (code structure plus consistent evidence, not exercised), **unverified** (cannot be checked from here).

---

## 1. Executive finding

**Yes. Production is a reduced GNSIS: it sees and hears, reasons, and answers in text and haptics only. It never speaks. Speech output, interruption with playback cancel, and ASR are implemented in this repository and absent from what the phone gets. Memory beyond the configured ~two-minute window has its mechanism implemented, but nothing in this repository writes the memories.**

The four findings that matter most, in order:

1. **GNSIS has no voice in production.** `generate_audio: false`, `init_tts: false`, no Talker device, no Token2wav assets configured, and the serving image does not contain the package that turns speech tokens into sound. The intended experience — "responds aloud, stays perceptive while talking" — is not exercised at all. (§5.1, §6)
2. **Even switched on, the phone could not play the one-GPU form of speech.** The native (in-model) Talker delivers audio riding on `chunk` messages; the phone client only treats `audio.chunk` as an audio header, so native audio bytes are silently dropped. The desktop client handles both. (§8)
3. **The full-duplex form of speech needs two GPUs and a GNSIS Talker checkpoint whose existence is unverified.** The detached Talker is the only path in which the Thinker keeps seeing and hearing while GNSIS speaks, and the only path that sends `playback.cancel`. Its loader refuses to run without a fine-tuned `tts.*` overlay, and the code forbids it on the Thinker's device. (§5.2, §6, §7)
4. **The "false speech" seen in the phone run is a diagnostic flag, not a decision.** `input_has_speech` is `rms ≥ 1e-4`, hard-coded, unreachable from YAML, and its only behavioural effect is how long the server drains after Stop. The model's own `listen/speak/interrupt` tokens are the real speech decision, and raw audio always reaches them. ASR, when enabled, feeds the task layer, not the Thinker's input. (§5.3)

Two more findings are not about what the product does, but should not wait:

- **The warm-up never runs.** The prefix cache and first-unit warm-up hang off a startup hook the model loader can no longer reach, so every session prefills the system prompt itself and each fresh container runs its first model unit inside someone's session. Production's own health at `ready` shows it: `prefix_cache_status: pending`. (§5.7)
- **Every session's raw microphone audio is written to the container's disk and is not deleted when the session ends.** (§5.6)

---

## 2. Current live architecture (what ran at 22:23 UTC)

```
iPhone Safari — runtime/minicpm_ft/mcpmft/infer/static/live.js
│ getUserMedia video{rear, 1280×720 ideal}                                   live.js:345
│ getUserMedia audio{echoCancellation, noiseSuppression, autoGainControl}     live.js:351-352
│ AudioWorklet 'minicpm-mic-capture' 2048-sample frames                       live.js:404-408
│   → linear resample to 16 kHz PCM16 → bare binary on /ws/duplex            live.js:283-295, 415
│   (no `audio.frame` header ⇒ no capture timestamps on audio)                (grep: none)
│ camera → canvas ≤640 px, JPEG q0.72, 1 fps, video_source=camera             live.js:361-402
│   → JSON header {frame_id, captured_at_ms} + JPEG bytes on /ws/screen
│ plays: `chunk` text (say), `haptic.cue` (device presets)                    live.js:509-535
│ would play: `audio.chunk`+binary, `playback.cancel`                         live.js:457-462, 503-508
▼
Cloudflare → Railway GNSISFRONTEND (Caddy, gnsis.studio) → /ws/* only
▼
Modal app gnsis-live · 1 × L40S · max_containers=1 · @modal.concurrent(max_inputs=4)
│                                                              modal/gnsis.py:140,150,156
│ CUDA_VISIBLE_DEVICES=0 · scaledown_window=60 · timeout 24 h                  modal/gnsis.py:148-149,161
│ image: torch/transformers/accelerate/av/librosa/…/fastapi/uvicorn/websockets
│        minicpm_ft + gnsis_runtime installed --no-deps                       modal/gnsis.py:89-114
▼
gnsis-serve --config runtime/configs/gnsis-live.yaml   (server.mode=lean, worker.provider=none)
│ DeferredRuntimeApp (port first, model loads behind it; inner startup hooks never run, §5.7)                      deferred_app.py
│ create_online_duplex_app → _Runtime{model_lock, sessions, prefix_snapshot}  online_duplex.py:915+
│ per session: GNSISDuplexSession (RLock) → DuplexLiveSession → OnlineRunner  duplex_bridge.py, realtime.py, online.py
│ model: MiniCPM-o 4.5 + /models/GNSIS/thinker, bf16, sdpa, init_vision+audio, init_tts=false
│                                                              gnsis-live.yaml:7-17,32
│
│ every 1000 ms unit (chunk_ms=1000):
│   newest camera frame (omni: reuse last if none) + 1 s audio → streaming_prefill
│                                                              realtime.py:444-475, screen.py:58-110
│   → streaming_generate → ≤7 text tokens; decision listen | speak | interrupt | tool
│                                                              online.py:470-498, realtime.py:527-676
│   → `chunk` JSON {text, is_listen, end_of_turn, interrupted, metrics{input_rms, input_peak,
│     context_window…}} → outbox → phone (input_has_speech is logged, not sent)                    online_duplex.py:681-696
│   audio_waveform is always None (generate_audio=false)                      online.py:496-497
│
│ context: sliding_window_mode=context_no_previous, context_max_units=128,
│          context_previous_max_tokens=0 → window configured for 128 one-second units, evicted units gone (§5.4)
│                                                              cli.py:77-79,88; context_window.py:36-75
│ coordinator: TaskToolsRealtimeCoordinator, provider none; mic PCM journaled to
│          /var/gnsis/media/<key>.input.pcm                    cli.py:646; task_tools_online.py:135-139,354+
│ limits: 15-min session cap, 15 s reconnect grace, model slot = 1 session   online_duplex.py:93, RECONNECT_GRACE_SEC
▼
22:23 run: ~200 frames accepted/consumed at 1 fps, text answers, ended with close 1006 (dropped, not Stop),
parked then released.  generate_audio=False · asr_enabled=False · detached_talker=False (§9 health)
```

## 3. Intended existing architecture (the fullest thing this repository can run)

```
Phone/desktop ──audio.frame{captured_at_ms}+PCM16──▶ /ws/duplex ──▶ Thinker (cuda:0)
            ──screen.frame{captured_at_ms}+JPEG──▶ /ws/screen ──▶ LatestScreenFrameBuffer
            ──PCM──▶ /api/asr/transcribe ──▶ Faster-Whisper sidecar (cuda:2 or cpu, or external)
            ──turn.final{final_asr, media}──▶ TaskToolsRealtimeCoordinator ──▶ task tools ──▶ worker (codex/ornith)
                                                  (transcript never enters the Thinker's input)          task_tools_online.py:154+

Thinker unit → text tokens ─┬─ native:   in-model Talker + Token2wav inside the SAME generate call (same GPU, same lock)
                            │            → 24 kHz PCM inline on `chunk`                       online.py:311-316; online_duplex.py:681-696
                            └─ detached: take_talker_condition() → AsyncTalkerWorker thread on cuda:1
                                         → Talker KV + Token2wav on its own device
                                         → SpeechSynthesisChunk / Done / PlaybackCancel
                                         → `audio.chunk` / `audio.done` / `playback.cancel`     realtime.py:611-618; detached_talker.py:578+; online_duplex.py:633-680

Context modes: context_no_previous (prod) | context_slate (pin task slate) | context_memory (+ memory.episode from a
memory service, summaries on eviction)                                                     pinned_context.py:476-503; online.py:443-453
Interruption: model emits <|interrupt|> → speech_worker.cancel("model_interrupt") → PlaybackCancel → outbox raises
audio floor, drops stale audio → phone `playback.cancel` → sources stopped                 online.py:482-495; realtime.py:569-573; online_duplex.py:758-762,823-832; live.js:506-508
Example of the full topology: runtime/gnsis_runtime/configs/serve.example.yaml — cuda "0,1" for server, Talker cuda:1, ASR cuda "2"
```

The desktop client (`static/app.js`) already speaks all of this protocol: timestamped audio frames (1508), ASR requests (1414), `turn.final` (901), `resume_token` (959, 1183), playback generations and `playback.cancel` (993–1003), and inline audio on `chunk` (1074–1078). The phone client speaks a subset (§8).

---

## 4. Capability matrix

Legend: ✅ LIVE + PROVEN · 🟡 IMPLEMENTED, OFF · 🟠 IMPLEMENTED, BLOCKED · 🔵 IMPLEMENTED, UNPROVEN · ⚪ INTENTIONALLY OUT OF MVP · ❌ MISSING

| Capability | Status | Prod config / health | Needs (assets · hardware · deps · client) | Tests | Evidence |
|---|---|---|---|---|---|
| Camera vision | ✅ | `media_mode: omni`, `allow_client_video: true`; health `vision_available: true`, `client_video.enabled: true`, `recommended_frame_rate: 1.0` | — | camera negotiation, frame accept/drop, privacy | gnsis-live.yaml:35-40; §9; `test_the_client_negotiates_camera_mode…`, `test_a_voice_session_takes_camera_frames_only_after_media_mode`; 22:23 run |
| Screen vision | 🔵 (desktop) / ❌ phone not wired | `client_video_sources: [camera, screen]` | phone client sends `video_source: camera` only | client screen-share lifecycle tests (desktop harness) | live.js:392; app.js; `tests/client/video-lifecycle.test.mjs` |
| Microphone raw audio → Thinker | ✅ | 16 kHz in, 1000 ms units | phone sends no `audio.frame` header ⇒ units untimestamped | flood/limit tests (stub Thinker) | live.js:404-421; online_duplex.py:2194-2235; realtime.py:241-262 |
| Speech detection (`input_has_speech`) | ✅ live, but **diagnostic only** | rms ≥ **1e-4**, not configurable | — | none | realtime.py:105,485; no `DuplexConfig` field (cli.py:41-97); `_build_session` never passes it (online_duplex.py:325-352) |
| Speech decision (model listen/speak/interrupt) | ✅ | tokens `<\|listen\|> <\|speak\|> <\|interrupt\|> <\|backchannel\|>` required at load | — | none at model level (Thinker stubbed) | online.py:781-832 |
| VAD | 🟡 | only inside the ASR sidecar (`vad_threshold 0.6`, `min_frame_rms 0.0035`, `min_peak 0.01`, `210 ms`) | ASR on | none | asr.py:32-70,160-164 |
| ASR (managed/external) | 🟡 + 🟠 | `asr.mode: disabled`; health `asr_enabled: false` | faster-whisper (not in image) · model dir on volume (unverified) · separate CUDA device or `device: cpu` or external · phone not wired | none | asr_process.py; cli.py:311-316; modal/gnsis.py:89-114; live.js (no ASR) |
| Transcript logging | model text ✅ · user speech ❌ | model text logged per unit | ASR for user text | log-trail test covers events, not text | realtime.py:658-674; `test_a_phone_session_leaves_a_reconstructable_log_trail` |
| Recent visual retention | ✅ (with a counter caveat, §5.4) | `context_max_units: 128` | — | prompt test | realtime.py:486-501; prompts.py RECENT VISUAL CONTEXT; `test_the_live_prompt_teaches_recent_visual_context`; 22:23 run |
| Previous conversational context | 🟡 | `context_previous_max_tokens: 0` (default), `context_no_previous` | a pinned mode together with `context_previous_max_tokens > 0` (the validator requires the pair, cli.py:224,231,239) and a mode the Thinker was trained for (unverified) | none | cli.py:77-79,88; context_window.py:97-101 |
| Memory episode channel | 🟡 mechanism · ❌ producer | health `memory_episode_channel: disabled` | `sliding_window_mode: context_memory` + something that writes episodes: the runtime asks the client for one (`memory.summary_needed`), the desktop client only logs the request, and nothing in this repository sends `memory.episode` | none | online.py:443-446; online_duplex.py:2444-2470; task_tools_online.py:146; app.js:1119-1123 |
| Thinker text response | ✅ | — | — | `test_the_client_renders_model_chunks` | 22:23 run |
| Native speech output | 🟠 | `generate_audio: false`, `init_tts: false`; health `generate_audio: false` | `minicpmo-utils[tts]` (not in image) · `token2wav_dir` + `ref_audio_path` on volume (unverified) · phone client change (§8) · base Talker with fine-tuned Thinker unvalidated (§5.2) | none | gnsis-live.yaml:15,33; common.py:76-88; load.py:191-200; pyproject `talker` extra |
| Detached speech output | 🟠 | health `detached_talker: false` | 2nd CUDA device (code-enforced) · `talker_checkpoint` with `tts.*` tensors (existence unverified) · `stepaudio2` · same assets as native | none | cli.py:365-383, build_app; detached_talker.py:137-186 |
| Token2wav | 🟠 | — | `stepaudio2.Token2wav` from `minicpmo-utils[tts]`; 25 tokens per emitted chunk | none | detached_talker.py:83-85,180-186 |
| Phone audio playback | 🔵 (detached) / ❌ (native) | — | `audio.chunk` path exists; `chunk`+audio not handled; playback AudioContext never `resume()`d — probably fine, §8 | none | live.js:298-333,457-462,503-505,546-575 |
| Listening while speaking | 🔵 detached / 🟠 native | — | detached: Talker thread, lock-free `poll_output`; native: Talker inside the locked generate | none | duplex_bridge.py:57-71,172-173; detached_talker.py:578+; online.py:470-498 |
| Seeing while speaking | same as above | frames consumed per unit under the same lock | — | none | realtime.py:453-456 |
| Interruption / barge-in | 🔵 detached · 🟠 native | — | see §5.5 | none | online.py:482-495; realtime.py:569-573; live.js:506-508,534 |
| Playback cancellation | 🔵 | — | only emitted in detached mode | none | online_duplex.py:647-653; live.js:506-508 |
| Haptics | ✅ (tests) · 🔵 in-session | tool `haptic` in health `tools` | — | 7 tests | online_duplex.py:487-550; test_live_surface.py |
| Session resume | 🟡 phone not wired | 15 s grace; `resume_token` in `ready` | phone ignores `resume_token`; desktop uses it | resume tests (server) | online_duplex.py:2004; live.js (no resume); app.js:959,1183 |
| Clean stop | ✅ tested · 22:23 run ended by drop (1006), not Stop | — | — | `test_end_says_stop_rather_than_dropping_the_socket` | live.js:613-673 |
| Action / worker layer | ⚪ | `worker.provider: none` | — | `test_gnsis_live_mvp_has_no_external_worker_dependency` | gnsis-live.yaml:52-53 |
| Warm-up / prefix cache | 🟠 never runs | health at ready: `prefix_cache_status: pending`; `prefix_prepare_seconds` and `first_unit_warmup_seconds` null | a startup hook the loader can reach — it calls `router.startup()`, which FastAPI 0.141.1 / Starlette 1.6.0 lack (reproduced) | none | online_duplex.py:1005-1008; deferred_app.py:246-249; §5.7 |
| Observability (session log trail) | ✅ | container-keyed logs on both channels | gaps in §11 | log-trail tests | online_duplex.py (#74–#77); `test_a_phone_session_leaves_a_reconstructable_log_trail` |

---

## 5. Hidden / off capabilities — why off, what enabling takes, hardware impact, safe for the next test?

### 5.1 Speech output — native (in-model) Talker

```
claim      GNSIS cannot speak in production; the native path is fully implemented but off, its runtime dependency is absent from the image, and its assets are unverified.
files      runtime/configs/gnsis-live.yaml:15,33 · modal/gnsis.py:89-114 · runtime/minicpm_ft/mcpmft/infer/common.py:76-88 · modeling/load.py:191-200 · minicpm_ft/pyproject.toml [talker]
symbols    model.init_tts, duplex.generate_audio, model.token2wav_dir, duplex.ref_audio_path, maybe_init_tts, DuplexParams.generate_audio
evidence   health 22:12 `generate_audio: False`; `uv_pip_install` list has no minicpmo-utils; `--no-deps` installs skip the extra
confidence proven (off, dependency missing) · unverified (assets on the volume)
```

- **Why off:** the MVP config chose perception-only (yaml header comment).
- **To enable:** `model.init_tts: true`, `model.token2wav_dir: <base>/assets/token2wav` (the example config's placement — verify), `duplex.generate_audio: true`, `duplex.ref_audio_path: <base>/assets/system_ref_audio.wav` (verify); add `minicpmo-utils[tts]>=1.0.6,<2` to the image's pip list (the `--no-deps` install will not pull it); phone client change (§8).
- **Hardware:** none beyond the current L40S for memory; **but** the Talker and Token2wav then run *inside* `streaming_generate` under the session lock, so each speaking unit pauses perception for the synthesis time (`cost_tts`, `cost_token2wav` in every chunk's metrics measure it). Strongly supported, not proven: that call returns the TTS costs, and the call itself is the model's remote code. Whether one L40S keeps 1-second units real-time while speaking is **unmeasured**.
- **Safe for the next test:** yes as a *degraded* test (§14 path A), once the assets are confirmed. Risk to name: if the production checkpoint is Thinker-only, as its name suggests (§5.2), the native Talker is the *base* MiniCPM-o Talker conditioned on fine-tuned hidden states — an unvalidated pairing.

### 5.2 Speech output — detached Talker (the full-duplex form)

```
claim      The detached Talker is the only implementation in which GNSIS keeps seeing and hearing while it speaks, and the only one that emits playback.cancel; it is blocked on a second GPU and a GNSIS Talker checkpoint.
files      runtime/gnsis_runtime/gnsis_runtime/cli.py:365-383,build_app · runtime/minicpm_ft/mcpmft/infer/detached_talker.py:91-96,137-186,578-785 · realtime.py:191-201,611-618 · duplex_bridge.py:57-71,172-173
symbols    duplex.detached_talker_device, duplex.talker_checkpoint, DetachedTalkerRuntime.from_thinker_model, AsyncTalkerWorker, take_talker_condition
evidence   from_thinker_model raises "No Talker tensors loaded" when the overlay contributes nothing (163-174); build_app raises "Thinker and detached Talker must use different devices"; _validate_gpu_assignment bounds the Talker index by server.cuda_visible_devices
confidence proven (requirements) · unverified (whether /models/GNSIS holds a talker checkpoint)
```

- **Why off:** no `detached_talker_device`, no `talker_checkpoint` in production config.
- **To enable:** `server.cuda_visible_devices: "0,1"`, `duplex.detached_talker_device: cuda:1`, `duplex.talker_checkpoint: /models/GNSIS/<talker>`, `model.token2wav_dir`, `duplex.ref_audio_path`, `talker_emit_speech_tokens: 25` (enforced), `minicpmo-utils[tts]` in the image, Modal `gpu="L40S:2"`. `model.init_tts` must be **true** in the persisted config: `validate_release_config` rejects `generate_audio: true` with `init_tts: false` at config load, before `build_app` runs (cli.py:181,205-206), and the same validator requires `ref_audio_path`, `talker_checkpoint` and `token2wav_dir` whenever a Talker device is set (cli.py:207-214). `build_app` then loads the *Thinker* with `init_tts=False` regardless (cli.py:525) — the detached runtime builds its own TTS on the second device — so `init_tts: true` costs no Thinker memory. `serve.example.yaml` shows exactly this pairing (lines 9, 41, 43).
- **Hardware:** two CUDA devices in one container. The SDK parses `gpu="L40S:2"` as type L40S, count 2 (`parse_gpu_config`, modal/_utils/function_utils.py:628 — run here); whether Modal grants two L40S per container is an account/plan entitlement — **unverified**.
- **Checkpoint:** the training code has a `talker` mode that trains `tts_proj`+`tts_decoder` *initialised from a thinker checkpoint* with `detach_llm_for_tts=always` (train/main.py:73-96) — i.e. the repository's own recipe is Thinker first, then a matching Talker overlay. The directory name `/models/GNSIS/thinker`, and the way the runtime uses it, match the `thinker` mode (`init_tts=false`, TTS frozen, `audio_loss_weight=0`), but which mode actually produced it is **unverified**: its saved training config, or whether its tensors include `tts.*`, would say (`modal volume ls "$GNSIS_MODELS_VOLUME" GNSIS/thinker`). **If no Talker overlay exists on the volume, the detached path is blocked on a training run, not on configuration.**
- **Safe for the next test:** only after both the checkpoint and the two-GPU grant are confirmed.

### 5.3 ASR (managed Faster-Whisper, or external)

```
claim      ASR is implemented end to end on the server and the desktop client, off in production, not buildable on the current image, and — crucially — it is not an input to Gander. Enabling it does not change what the Thinker hears.
files      asr_process.py:22-40,88-183 · mcpmft/infer/asr.py:32-70,129-165 · online_duplex.py:1199-1258,2431-2443 · task_tools_online.py:154-290 · cli.py:311-316,384-398 · modal/gnsis.py:89-114
symbols    asr.mode, AsrService, /api/asr/transcribe, turn.final, bind_final_turn, TurnEnvelope.final_asr
evidence   feed_pcm16 always prefills the waveform regardless of ASR (realtime.py:444-475); bind_final_turn records a TurnEnvelope for task tools and attaches media; nothing feeds final_asr to OnlineRunner
confidence proven
```

- **Raw audio + transcript:** with ASR on, the Thinker still receives the raw microphone stream every unit — proven. The transcript goes to `TaskToolsRealtimeCoordinator` (turn binding for `task_start`/`task_send`, media attachment, gateway instruction text at gateway.py:843) — with `worker.provider: none` nobody consumes it. There is **no regression to transcript-only interaction possible** in this architecture, and also **no "transcript for language/turn semantics" reaching Gander**: that idea is not implemented. The only route from a transcript back to the model is the action layer — a task the model starts takes the latest bound turn's transcript as its instruction (gateway.py:843), and task state reaches the model through the task slate — and production has no worker, while the validator forbids exposing the slate in `context_no_previous` (cli.py:246). The listen/speak decision is the model's own, except for a session's first `force_listen_count` units, which the upstream model forces to listen by count, not by sound (online.py:761-764,573).
- **Would the sidecar's gating have prevented the phone run's false positives?** It would have refused to *transcribe* near-silence (adaptive threshold ≥ 0.0035 RMS, peak ≥ 0.01, ≥ 210 ms active), but it never touches `input_has_speech` or the model. Proven: asr.py:45-70 is only reached via `/api/asr/transcribe`.
- **To enable for diagnostics:** `asr.mode: managed`, `asr.model_path: /models/<faster-whisper dir>` (unverified on the volume), `faster-whisper>=1.2,<2` added to the image (pulls CTranslate2), and either `asr.device: cpu` (`compute_type: int8` advisable; code allows `cpu`, asr_process.py:30 / asr.py:326) — **no GPU-assignment constraint applies to CPU** (cli.py:384-385) — or a separate CUDA device. Or `asr.mode: external` with a URL, which has no device constraint at all. The **phone client would still send nothing to it**; only the desktop client does. A phone transcript for diagnosis needs a small client addition (buffer PCM per model listen/speak boundary and POST it, as app.js does).
- **Hardware:** no GPU, on CPU or external. On CPU, though, it shares the container with the runtime, and the function sets no `cpu=` (modal/gnsis.py:139-157): check the container's CPU allocation before running a large Whisper model there. External avoids the question.
- **Safe for the next test:** as a side channel, yes; it cannot affect the model.

### 5.4 Context and memory beyond the configured ~two-minute window

```
claim      Production keeps a rolling window of about 128 seconds of multimodal units and nothing older; evicted units leave no text summary; the two richer modes are implemented and off; the mode the Thinker was trained for is not recorded in the repository.
files      cli.py:77-79,87-88 · context_window.py:10-75,97-101 · pinned_context.py:93-505 · online.py:186-196,443-464 · gnsis-live.yaml:43-44
symbols    sliding_window_mode, context_max_units, context_previous_max_tokens, install_context_no_previous, install_context_memory, install_context_slate, runtime_window_mode_for_training
confidence proven (behaviour of each mode) · unverified (training layout; exact eviction trigger inside the model's remote StreamDecoder)
```

In product terms:

| Mode | What Gander can remember | What it cannot |
|---|---|---|
| `context_no_previous` (**prod**) | Everything inside the live window: recent frames, recent audio, its own recent words — "the one before", "which was first" work while both are inside ~128 s | Anything evicted: no `previous:` text is kept (`_clear_previous_state`), `context_previous_max_tokens=0`. If the window is the configured 128 units, a 15-minute session has forgotten minute 1 by about minute 3. |
| `context_slate` | Same window plus a pinned task slate the runtime maintains (action layer) | Same eviction; no episodic memory |
| `context_memory` | Same window plus **memory episodes** pinned in protected context; on eviction the runtime asks for a summary (`_request_summary`) and can replace evicted captures with an episode | Requires something to write the episodes. The runtime asks the client for a summary (`memory.summary_needed`); the desktop client only logs the request, and **nothing in this repository sends `memory.episode`** — the docstring names an external MM-Mem service (realtime.py:225-226) |

- `memory_slate_max_tokens: 256` in production is inert: it is only read when a pinned mode is installed (online.py:291,304).
- **Retention diagnostics caveat:** `_total_dropped_units` is maintained by the pinned controller (pinned_context.py:344-347); the `context_no_previous` eviction path (`_drop_unit_without_rebuild`) does not touch it. Unless the model's remote decoder increments it, **`dropped_units=0` in the phone run does not prove nothing was evicted** — read `context_units`, `oldest_unit`, `newest_unit` from the `screen frame accepted` / `visual unit retained` lines instead (§11). The phone run makes this concrete: it stepped at least 200 units (each consumed frame belongs to one) and still reported `dropped_units=0`. Either the counter is not maintained in this mode, or nothing was evicted — which would mean the window is longer than configured. Those log fields settle which: if `context_units` stops near 128 while `oldest_unit` advances, the window is the configured ~128 s.
- **Do not switch modes for the next run.** `runtime_window_mode_for_training` maps a training layout to a runtime mode (`window_no_previous → context_no_previous`, `context_memory`, `context_slate`); no training config is checked in, so the Thinker's layout is **unverified**. Check the checkpoint directory for its training config: `modal volume ls "$GNSIS_MODELS_VOLUME" GNSIS/thinker`. The training schema's defaults (`sliding_window_training: off`, `kv_keep_previous_units: 128`, mcpmft/data/collator.py:55-56) match production's 128-unit window, but say nothing about the layout this checkpoint used. If a mode is ever switched, the validator also requires `context_previous_max_tokens > 0` for the pinned modes and `expose_task_slate_to_model: true` for `context_slate` (cli.py:224,231,239).

### 5.5 Interruption / barge-in

```
claim      The complete path exists only for the detached Talker. In native mode the model stops producing speech within one unit but the phone is never told to cancel what it already has.
files      online.py:482-495,846-861 · realtime.py:569-573,376-384 · detached_talker.py:635-653 · online_duplex.py:647-653,758-762,823-832 · live.js:506-508,534 · app.js:1048-1052
confidence proven (code paths) · unproven (never exercised; no test)
```

Step by step, detached mode: (1) GNSIS speaking — Talker thread synthesising, pump sending `audio.chunk` at AUDIO priority; (2) user speaks — mic frames keep arriving, `feed_pcm16` steps the Thinker under the lock (Talker does not hold it); (3–4) the Thinker emits `<|interrupt|>` → `is_interrupt` (online.py:482-495) — **the model is the speech detector, from raw audio**; (5) `speech_worker.cancel("model_interrupt")` bumps the generation, `PlaybackCancel` is queued, the outbox raises its floor and discards stale audio, `playback.cancel` reaches the phone, `live.playback.cancel()` stops every scheduled source; (6) `awaiting_reply=True`, `_assistant_turn_open=False`; the next units are answered against the current window (frames included); (7) the cancel has already moved the Talker to a new generation, so the answer's audio passes the outbox's floor and plays. `stop_reply_wait_sec` (8 s, not configurable, realtime.py:104) bounds only the post-Stop drain; it plays no part in interruption. Native mode: steps 1–4 and 6–7 hold; at step 5 the interrupted unit carries `audio_waveform=None` and `_reset_after_interrupt` resets Token2wav, but **no `playback.cancel` is sent** — the desktop stops on `chunk.interrupted` (app.js:1048), the phone only clears its text (live.js:534). Tail audio on the phone: whatever is already scheduled — about one unit if synthesis keeps pace with playback, more if audio has queued ahead.
Client-initiated `break`/`clear_break` and `reset` also exist (online_duplex.py:2293-2336, 2413-2418); the phone sends none of them.

### 5.6 Not a capability, but found on the way: the raw-mic journal

```
claim      Every session's microphone audio is appended to the container's disk and left there when the session ends.
files      cli.py:646 (media_dir = runtime_dir/"media") · task_tools_online.py:135-139,354-370,686-697 · online_duplex.py:2199
symbols    TaskToolsRealtimeCoordinator._audio_path, record_pcm16, _close_audio
evidence   record_pcm16 is called for every binary frame; _close_audio only fsyncs and closes; unlink happens only on close(discard_state=True). All three call sites checked: normal end (online_duplex.py:2666) and grace expiry (1904) close without it; only `reset` (2308) deletes
confidence proven
```

It exists to attach audio to `turn.final` media for the task layer, which is off. The camera equivalent was closed by `persist_camera_frames=false`; the audio equivalent was not. The disk is the container's own and disappears when it scales down, but a container that stays warm across several sessions keeps every one of them until then. Recommend: skip the journal when no worker provider is configured, or unlink on every close.

### 5.7 The warm-up never runs

```
claim      The prefix cache and the first-unit warm-up have never run in production. Every session prefills the system prompt itself, and the first session on each fresh container runs the process's first model unit live.
files      runtime/gnsis_runtime/gnsis_runtime/online_duplex.py:365-415,1005-1008 · deferred_app.py:246-249 · realtime.py:197 (per-session fallback)
symbols    _prepare_static_prefix, app.router.add_event_handler("startup", …), DeferredRuntimeApp._load_runtime, Router.startup
evidence   Production health at ready (task e6c066f4): prefix_cache_status "pending", prefix_prepare_seconds and first_unit_warmup_seconds null. _prepare_static_prefix sets "ready" before it returns, and the loader awaits startup before it declares ready, so at ready the hook had not run.
           Cause reproduced here with FastAPI 0.141.1 / Starlette 1.6.0, the versions the worker image resolved on 2026-09-22: the handler registers, getattr(router, "startup") is None, the handler never runs. Starlette 1.6.0's routing.py has no startup, add_event_handler or on_startup; FastAPI keeps add_event_handler but not startup.
confidence proven (it did not run in production) · strongly supported (why — the Modal image's exact FastAPI version is not visible from here)
```

- **What it costs the person:** each session waits for its own system-prompt prefill before `ready`, and on a freshly started container the first answer also carries the one-off GPU warm-up — the two costs the cache exists to remove. Neither is measured in production, because the measurements live in the code that never ran. On Path B it also means `warm_token2wav` never runs, so the first spoken chunk on each container pays the vocoder's warm-up.
- **Fix (proposed, not made):** have the loader start the inner app through its ASGI lifespan, or call the prefix preparation directly once `build_app` returns — with the test in Appendix C, item 11.
- **Health will not reach `prefix_cache_status: ready` until this is fixed.** §9 accounts for that.

### 5.8 Smaller items

- **Session resume** — server complete and tested; the phone never sends `resume_token`, so a dropped socket is "Connection lost" and the parked Thinker holds the slot for 15 s for nobody. The desktop uses it.
- **Timestamped audio** — the phone sends bare PCM; frames are matched to units by arrival ("newest pending"), not by capture time (screen.py:70-73). Adequate at 1 fps; the desktop's `audio.frame` headers give the precise alignment.
- **Screen share on the phone** — server and desktop support it; the phone hard-codes `video_source: 'camera'`.

---

## 6. Blockers

| Kind | Blocker | Proof | How to clear |
|---|---|---|---|
| Config | `generate_audio`, `init_tts`, `token2wav_dir`, `ref_audio_path` unset | gnsis-live.yaml | §14 |
| Config | `input_speech_rms` not reachable from YAML | cli.py `DuplexConfig` has no field; `_build_session` does not pass it | add the field, forward it, and log it in health |
| Runtime | the prefix cache and first-unit warm-up never run | health at ready; reproduced (§5.7) | small loader change + a test (Appendix C, 11) |
| Missing assets (unverified) | Token2wav directory, reference voice WAV, GNSIS Talker checkpoint, Faster-Whisper model | not referenced by prod config; `cache_gnsis_models` validates only base + thinker (modal/gnsis.py:125-138) | `modal volume ls "$GNSIS_MODELS_VOLUME" MiniCPM-o-4_5/assets` · `modal volume ls "$GNSIS_MODELS_VOLUME" GNSIS` (run with the worker's `GNSIS_MODELS_VOLUME` value; read-only) |
| Missing dependencies | `minicpmo-utils[tts]` (stepaudio2 / Token2wav) — **missing**; `faster-whisper` + CTranslate2 — **missing**; both are optional extras skipped by `--no-deps` | pyproject `[project.optional-dependencies]`; modal/gnsis.py:89-114 | add to `uv_pip_install` explicitly |
| GPU / resource | detached Talker needs a second CUDA device; managed CUDA ASR a third (or CPU / external) | cli.py:365-398; build_app device check | `gpu="L40S:2"` — grant unverified |
| Client (phone) | native audio on `chunk` not played; no `resume_token`; no ASR/`turn.final`; no `audio.frame` headers; AudioContext created after `await`, never resumed | live.js:456-462,546-575 | small live.js changes; vendored copy refreshed in GNSISFRONTEND per its README |
| Model / checkpoint | native path pairs base Talker with a fine-tuned Thinker (unvalidated); detached path requires a Talker overlay that may not exist; Thinker's training window layout unrecorded | train/main.py:60-96; detached_talker.py:163-174 | inspect `/models/GNSIS`; if no talker, plan the `talker` training stage |

---

## 7. Full-duplex verdict

**see + listen + reason + speak + keep seeing/listening + accept interruption — the existing implementation can deliver all six, unchanged, in exactly one topology: Thinker on cuda:0, detached Talker + Token2wav on cuda:1 (two GPUs in one container), a GNSIS Talker checkpoint on the volume, `minicpmo-utils[tts]` in the image, Token2wav assets and a reference voice on the volume, and the phone's existing `audio.chunk`/`playback.cancel` handlers.** Confidence: strongly supported for the concurrency claim (lock structure), proven for the requirements, unproven end to end (no test exercises audio, and it has never run in production).

On **one L40S**, unchanged code delivers *see + listen + reason + speak* with the native Talker, degraded in three specific ways: perception pauses during each unit's synthesis (same lock), interruption has no cancel signal (≤ ~1 s of tail audio), and the phone needs a client change to hear anything. Real-time headroom with speech on one GPU is unmeasured; the per-unit `cost_*` metrics answer it in the first minute of a test.

Answers to §5 A–F of the brief: **A** two GPUs (Thinker + detached Talker); ASR is not part of the Gander experience and needs no GPU. **B** everything except speech, plus native speech with the degradations above. **C** the detached Talker — by code, not preference (`build_app` refuses same-device; `_validate_gpu_assignment` bounds it). **D** nothing; a third GPU only for managed CUDA ASR, which can run on CPU or externally. **E** yes — `asr.device: cpu` or `asr.mode: external`; neither reduces the Gander path because ASR does not feed the Thinker's input (on CPU, mind the container's CPU, §5.3). **F** the detached Talker is built in-process from the Thinker model and receives hidden-state tensors per unit by direct call (`take_talker_condition` → `AsyncTalkerWorker.submit`); there is no serialisation or network contract, so today it must live in the same process. Splitting it out is new work.

---

## 8. Live client audit (phone: `live.js`)

- **If the server emitted speech tomorrow, would the iPhone page play it?** *Detached mode:* the path exists — `audio.chunk` header → binary → `playback.play` at the header's sample rate; `playback.cancel` stops all sources. Unproven. The playback `AudioContext` is created in `start()` after `await openCamera()` (live.js:546-573) and never `resume()`d, which would be a concern on iOS — except the microphone's AudioContext is created later still, in the `ready` message handler with no gesture at all (live.js:404), and it demonstrably ran in the 22:23 session: every consumed frame belongs to a model unit, and units only step when microphone audio arrives. That is good evidence WebKit lets this page start audio while capture is active. A `resume()` is still cheap insurance. *Native mode:* **no** — audio arrives after a `chunk` message, which never sets `live.pendingAudio` (only `audio.chunk` does, 503-505); the bytes are dropped at 457-462. The desktop handles it (app.js:1074-1078). Fix is a few lines: treat `chunk` with `audio: true` as a pending header.
- **If the user speaks while GNSIS is talking, does the mic keep sending?** Yes — proven: capture runs in its own AudioContext, is not connected to the destination (404-421), and nothing in the message handler pauses it.
- **Will playback leak into the mic?** Not provable from code. Mitigations in place: `echoCancellation: true`, `noiseSuppression: true`, `autoGainControl: true` requested (351-353); mic never routed to speaker. Whether Safari's AEC references page-generated audio is a platform behaviour to observe in the test (watch for the model interrupting itself or answering its own words).
- **Where does the sound come out?** On some iOS versions, audio played while the microphone is open is routed to the earpiece rather than the loudspeaker. Not provable from code; check it at step 3 of §10.
- Wired: camera negotiation via `media.mode`, frames with `captured_at_ms`, `chunk` text, `haptic.cue`, `stop`→`session.done` (1.5 s bound), `pagehide`→stop. **Not wired:** `audio.frame` headers, `audio.chunk` on `chunk`, `resume_token`, ASR, `turn.final`, `break`/`reset`, screen source, `visibilitychange`.

---

## 9. Expected health payload before the phone session

Baseline is the 22:12 UTC smoke health from task `e6c066f4` (GNSISWORKER logs), which is what production reported at ready:

```
status: ok · runtime_state: ready · vision_available: true
client_video: {enabled: true, mode: omni, recommended_frame_rate: 1.0}
input_sample_rate: 16000 · output_sample_rate: 24000 · chunk_ms: 1000
generate_audio: false · detached_talker: false · asr_enabled: false
sliding_window_mode: context_no_previous · memory_episode_channel: disabled
context_max_units: 128 · context_previous_max_tokens: 0 · task_slate_visible_to_model: false
prefix_cache_status: pending · prefix_cache_tokens: 0 · first_unit_warmup_seconds: null
tools: [haptic, task_start, task_send, task_resolve] · busy: false · active_sessions: []
```

For the next-run target, the fields that must read differently:

| Field | Path A (one GPU, native) | Path B (two GPUs, detached) |
|---|---|---|
| `generate_audio` | **true** | **true** |
| `detached_talker` | false | **true** |
| `prefix_cache_status` | stays `pending` until §5.7 is fixed, then **ready** | same |
| `first_unit_warmup_seconds` | null until §5.7 is fixed, then a number | same (Token2wav warm-up) |
| `asr_enabled` | true only if a diagnostic transcript is wanted (CPU/external) | same |
| everything else | unchanged | unchanged |

Fields health should expose but does not today: whether a Talker checkpoint was loaded, `token2wav_dir`, `input_speech_rms`, the deployed git commit. Proposed in §15.

---

## 10. Phone acceptance test

Same phone, same room, one session. Say each line naturally; wait for the reply before the next unless the step says otherwise.

1. Start, allow camera + mic. Expect "Session ready." within seconds of the first frame; feel the success haptic.
2. Say nothing for 10 s, then: **"Are you still there?"** — expect a spoken reply; note whether it also replied during the silence (false speech).
3. **"What's two plus two?"** (non-visual) — spoken answer, text mirrors it. Note the volume, and whether sound comes from the loudspeaker or the earpiece (§8).
4. Hold up object A (e.g. a keyboard): **"What is this?"** — names it.
5. Hold up object B (a remote): **"And this?"** — names it.
6. Put both down: **"Which one did I show you first?"** — must say A, from recent context, not the current frame.
7. Ask something long ("Tell me everything you noticed in this room") and **interrupt mid-sentence with "Stop — what colour is the remote?"** — old speech must cut within a second (detached) or tail off within ~1 s (native); the answer must address the remote.
8. While it is answering, **pan the camera to the window** and ask **"What do you see now?"** — must describe the new view (proves seeing while speaking).
9. Count the words it heard: read a sentence of 12 words; ask it to repeat. Compare to what you said (transcript fidelity without ASR = the model's own hearing).
10. Speak while it speaks, without a question ("uh-huh… right…") — observe whether it stops; then ask a real question and observe whether it stops for that. No code decides this: it is the model's trained behaviour, which is what this step tests.
11. Press End. Expect "Session ended." and the phone's camera/mic indicators off.

Pass/fail is per step; write the time of each step down so §11 can align it with logs.

---

## 11. Logs to pull after the test

**Modal** (`modal app logs gnsis-live`, filter on the `session_id` from the phone's `ready`): `websocket connected: … channel=duplex session_id=` · `duplex ready: … generate_audio=` · `media mode requested/applied` · `screen ready` · every `screen frame accepted: … context_units= visual_units= oldest_unit= newest_unit= dropped_units=` · every `visual unit retained: consumed_frame_ids=` · every `duplex chunk=N decision=listen|speak|interrupt|tool unit_id= prefill_mode=OMNI|AUDIO input_rms= input_peak= input_has_speech= text=` (this is the model-side transcript of what it said and when it decided to speak) · `Detached Talker cancelled generation= … reason=` and `Detached Talker produced no audio` (path B) · `websocket outbound … queue_ms= send_ms=` (playback latency) · `duplex disconnected: … close_code= reason= parked=` · at startup `Prepared static GNSIS prefix cache`, `Warmed …`, and `build_app: load_for_infer failed` if anything went wrong.
**Per-unit metrics** (in each `chunk`): `cost_llm`, `cost_tts`, `cost_token2wav`, `cost_all` — the real-time budget question for path A; `context_window.unit_count/visual_units/oldest_unit_id/newest_unit_id`.
**Worker** (Railway GNSISWORKER): the `deploy_live_runtime … succeeded` line with the health document and the commit of the worker deployment that ran it — this is today's only record of *which commit* was deployed (§15 asks to make it explicit).
**Phone:** `live.stats` (frames sent/accepted/dropped, audio messages) is kept in memory and never reported — add one `console`/log line at stop, or send it in the `stop` control.
**Not available today:** what the user said (no ASR), which the §10 script compensates for by having you write it down.

---

## 12. Only-after-failure alternatives

Only if the §10 run shows the existing component inadequate:
- **Talker / TTS:** if base-Talker speech on the fine-tuned Thinker is unintelligible or drifts, the in-architecture fix is the repository's own `talker` training stage (an overlay initialised from the Thinker), not an external TTS — an external TTS would break "keep seeing/listening while speaking" and the interrupt contract.
- **VAD / speech decision:** the model's listen/speak/interrupt tokens are the VAD. If they fire on silence in the real room, the first lever is the Thinker (data/threshold), not a separate VAD; a separate VAD cannot gate what the model hears without changing the unit protocol.
- **ASR:** Faster-Whisper is already integrated and gated; swap only if its transcripts of the §10 sentences are wrong.
- **Another omni model:** no basis in this audit.

---

## Appendix A — the brief's audit sections, cross-referenced

- §1 pipeline diagram → report §2–§3. §2 config audit → §4 matrix + §5. §3 ears → §5.3 and the `input_has_speech` rows (value `1e-4`, realtime.py:105; effect: `awaiting_reply` → drain length after Stop only — its sole reader is realtime.py:339, checked across the runtime; the phone's Stop waits 1.5 s regardless, live.js:634). §4 mouth → §5.1–§5.2, §7. §5 topology → §7 (A–F). §6 artifacts → §6 table. §7 dependencies → §6 table. §8 client → §8. §9 barge-in → §5.5. §10 memory → §5.4. §11 checkpoints → §5.2 (training modes), tokens: the load requires `<tool_call> </tool_call> <tool_response> </tool_response> <|chunk_eos|> <|turn_eos|> </unit> <|listen|> <|speak|> <|interrupt|> <|backchannel|>` (online.py:781-832); `<|backchannel|>` is GNSIS-added and mean-initialised if the checkpoint's tokenizer lacks it (tokenizer_tools.py:42, load.py:404-425) — whether the production checkpoint ships it trained is **unverified** (`modal volume ls "$GNSIS_MODELS_VOLUME" GNSIS/thinker` and read `added_tokens.json`). §12 matrix → §4. §13 → honoured (no new models proposed outside §12). §14 → below. §15 tests → below. §16 safety → honoured.

## Appendix B — §14: smallest next-run configuration

**Decision hinge, before anything else:** `modal volume ls "$GNSIS_MODELS_VOLUME" GNSIS` — is there a Talker checkpoint next to `thinker`? And `modal volume ls "$GNSIS_MODELS_VOLUME" MiniCPM-o-4_5/assets` — are `token2wav/` and the reference WAV there?

**Path B — the full experience (requires a second GPU; every step justified above):**
1. Image: add `"minicpmo-utils[tts]>=1.0.6,<2"` to `uv_pip_install` (modal/gnsis.py).
2. Modal: `gpu="L40S:2"`. Nothing else in modal/gnsis.py: the runtime sets `CUDA_VISIBLE_DEVICES` from `server.cuda_visible_devices` itself before loading (cli.py:690), overriding the `setdefault("0")`.
3. Config: `server.cuda_visible_devices: "0,1"`, `duplex.detached_talker_device: cuda:1`, `duplex.talker_checkpoint: /models/GNSIS/<talker>`, `model.token2wav_dir`, `duplex.ref_audio_path`, `duplex.generate_audio: true`, `duplex.talker_emit_speech_tokens: 25`. Set `model.init_tts: true` — config validation refuses `generate_audio: true` without it (cli.py:205-206); `build_app` still loads the Thinker itself without TTS (cli.py:525).
4. Preflight: extend `cache_gnsis_models` to assert the four new paths.
5. Phone: `context.resume()` fix (recommended, not required for the protocol).
6. Deploy via the internal path; wait for health `generate_audio: true` and `detached_talker: true` (`prefix_cache_status` will read `pending` until §5.7 is fixed); run §10.

**Path A — one L40S, degraded but useful (if no Talker checkpoint, or no two-GPU grant):**
1. Image: same package.
2. Config: `model.init_tts: true`, `model.token2wav_dir`, `duplex.generate_audio: true`, `duplex.ref_audio_path`.
3. Phone: treat `chunk` with `audio: true` as an audio header; `context.resume()`. Refresh the vendored copy in GNSISFRONTEND.
4. Preflight as above (token2wav + WAV).
5. Deploy; health `generate_audio: true`, `detached_talker: false`; run §10 and read `cost_tts`/`cost_token2wav`/`cost_all` per unit. **Degraded:** perception pauses during synthesis; no `playback.cancel`; base Talker on a fine-tuned Thinker.

Both paths: leave `sliding_window_mode`, ASR and the worker as they are. Fixing §5.7 first is optional but cheap: without it, the first answer on each fresh container carries the warm-up. Add an `input_speech_rms` config field only if you want the diagnostic flag to mean something (0.0035, the ASR floor, is the obvious value); it changes nothing the model does.

## Appendix C — §15: tests to add before a live deploy (none of these exist)

The harness stubs the Thinker with `poll_output → None` and no Talker (conftest.py). No runtime test (84 collect in this sandbox; three more files need the package installed to import) and no client test sends an audio byte to the client — checked by searching every test for `audio.chunk`, `SpeechSynthesis*`, `PlaybackCancel`, `playback.cancel`, received bytes and `generate_audio=True`.

1. **Config intent:** a test that loads `gnsis-live.yaml` and asserts each subsystem's state explicitly — `asr.mode`, `generate_audio`, `init_tts`, `detached_talker_device`, `sliding_window_mode` — so a default can never silently decide it.
2. **Deploy-time assets:** `cache_gnsis_models` asserts base, thinker, and — when speech is configured — token2wav dir, reference WAV, talker checkpoint; a unit test for the resolver.
3. **Health completeness:** health includes `talker_checkpoint_loaded`, `token2wav_dir`, `input_speech_rms`, `git_commit`; test asserts the keys.
4. **Audio reaches the client:** a `StubThinker` variant whose `poll_output` yields `SpeechSynthesisChunk`, `SpeechSynthesisDone`, `PlaybackCancel`; assert the socket receives `audio.chunk` + a binary frame of `audio_bytes` length at 24 000 Hz, then `playback.cancel` with the right generation, and that stale audio below the floor is dropped.
5. **Native inline audio:** a stub event with `audio_waveform` set; assert `chunk` carries `audio: true` and a binary follows.
6. **Client plays it / mic continues / camera continues / cancel stops sources:** extend `tests/client/harness.mjs` with a fake duplex that emits the above; assert `playback.play` is called with the header's rate, capture messages keep flowing during playback, frame ticks keep firing, and `cancel()` stops sources.
7. **Interrupt cancels:** stub emits an `interrupted` chunk (native) and a `PlaybackCancel` (detached); assert the client clears text (both) and stops playback (detached, and native once §8's fix lands).
8. **Transcript to turn.final:** post PCM to `/api/asr/transcribe` against a fake sidecar; send `turn.final`; assert `turn.final.accepted` and that `feed_pcm16` was still called for the same audio.
9. **False silence:** feed 5 s of −80 dBFS noise; assert no user turn is created (there is no such thing today — this test pins that) and that `input_has_speech` reflects the configured threshold.
10. **Diagnosability:** already covered by `test_a_phone_session_leaves_a_reconstructable_log_trail`; add the deployed-commit line.
11. **Startup hooks run:** build the real app under `DeferredRuntimeApp` with a stub loader and assert `prefix_cache_status` is `ready` when health first reports `ready` — the test that would have caught §5.7.

The final gate remains the §10 phone session.
