# GNSIS 0.5 local stack — benchmarks and evaluation

Measured 2026-10-18/19 on the GNSISBACKEND `main` line (post-#198 phonon ASR,
post-#200 schema-mode decide). Everything below was run against the real
shipped code path — the same classes `local_controller_serve` hosts — not a
re-implementation.

## What the 0.5 local pipeline is

```
spoken words
  → phonon-2 ASR            (/v1/transcribe, fermion-research, MLX on Mac)
  → OmniJev 0.8B decide     (/v1/decide — schema-derived ballot + system_one
                             vision judgment on the live screen still)
  → host action dispatch    (open/files/browser/input/window/clipboard)
  → Kitten TTS              (/v1/speak)
```

All behind `local_controller_serve.py` on `127.0.0.1:8766`, reached by the
desktop only when `GNSIS_LOCAL_CONTROLLER_URL` + `GNSIS_LOCAL_CONTROLLER_TOKEN`
are set. No cloud dependency anywhere in the loop; screen stills come from the
already-active screen share, never a fresh screenshot.

## Decide benchmark — the card numbers

Same task on every system: spoken request "click the Apple menu" + the
`desktop-v5` tool manifest + a real desktop still. Judge picks one candidate.

| Runner | Time / decide | Cost / decide | Notes |
|---|---|---|---|
| **OmniJev 0.8B — Mac CPU (16 GB, no fast kernels)** | **~21 s** | ~free (local power) | one `system_one` pass; conf 0.94, grounded args |
| OmniJev 0.8B — old generate path, Mac CPU | ~6.5 min | — | retired default; `GNSIS_LOCAL_CANDIDATE_MODE=generate` |
| OmniJev 0.8B — GPU (L4) | _pending spend limit_ | _pending_ | bench staged: `~/local05/omnijev_gpu_bench.py` |
| Gemini 2.5 Flash (hosted) | ~1.3 s | $0.00034 avg | picked correctly, conf 1.0 |
| Gemini 2.5 Flash Lite (hosted) | ~1.9 s | $0.000145 | picked correctly |
| Claude Haiku 4.5 (hosted) | ~2.2 s | $0.0012 | picked correctly, cited the menu visually |
| GPT-5 mini (hosted) | ~4 s | $0.0002 | returned empty (no usable JSON) |

Modal GPU rate used for cost math: L4 $0.80/hr → $0.00022/s;
T4 $0.59/hr; A10G $1.10/hr; L40S $1.95/hr. A ~1 s GPU decide ≈ $0.0002 —
same order as the cheapest hosted call.

## Per-stage timings, real Mac (16 GB)

| Stage | Cold | Warm | Detail |
|---|---|---|---|
| phonon-2 transcribe | 17.5 s (first call) | **1.8 s** | ~2 s utterance → text; accurate |
| OmniJev decide (schema mode) | ~27 s incl. warmup | **~21 s** | conf 0.94 on grounded pick |
| OmniJev decide (generate mode) | ~6.5 min | ~6.3 min | only kept for GPU hosts |
| Kitten speak (short reply) | 38.9 s (incl. 1.9 GB dl) | **~22 s** | playback confirmed in-call |
| action dispatch | <4 s | — | policy check + lease + actuator |

End-to-end observed: utterance → transcribed 11.3 s → decided 27 s →
action dispatched → spoken reply chunk. Live call ran clean through.

## Resource footprint

- Disk: Qwen3.5-0.8B 3.3 GB + OmniJev ckpt 238 MB + phonon-2 169 MB +
  Kitten 1.9 GB ≈ **5.6 GB** total.
- Service RAM: ~4.5 GB resident.
- Decide CPU: 280–516 % while thinking (before the disconnect-cancel fix it
  kept burning orphaned CPU after the client hung up — now HTTP 499).

## Why decide is the bottleneck, in one line

`mso/infer.py` picks `cuda` else `cpu`; `fast_kernels.py` wraps `fla.ops`
Triton kernels that only exist for CUDA. On a Mac there is no installable
speed-up — the ~21 s is the reference PyTorch path on CPU. GPU removes the
wall (kernels exist + memory bandwidth); MLX is the possible Mac-side lever.

## Known gaps (not in these numbers)

- The `input` click actuator returns `not_found` when it can't resolve a
  spoken label like "Apple menu" to a real control — grounding-layer work,
  separate from decide speed.
- The dock pill can be dragged under the macOS Dock strip where it becomes
  unreachable (`localStorage['gnsis.place']` holds the offset).
- 1.0 cloud voice is 404ing at `wss://gnsis.studio/ws/duplex` — unrelated
  to local 0.5.

## Reproduce

Local service (Mac):
```
GNSIS_LOCAL_CONTROLLER_TOKEN=$(cat ~/local05/token.txt) \
GNSIS_OMNIJEV_SOURCE=~/local05/models/omnijev-source \
GNSIS_OMNIJEV_CHECKPOINT=<hf snapshot>/OmniJev-0.8B \
GNSIS_QWEN_BASE_MODEL=<hf snapshot>/Qwen3.5-0.8B \
~/local05/venv/bin/gnsis-local-controller    # loopback :8766
```

Decide curl: `POST /v1/decide` with
`{requestId,text,step:0,history:[],tools,tool_schemas,state_image?}`.

Hosted-VLM side: `/tmp/vlm_bench2.py` — same still + same candidates through
`openrouter.ai/api/v1/chat/completions`, 3 reps per model, temperature 0.

GPU side: `~/local05/omnijev_gpu_bench.py` —
`modal run` on `gpu="L4"`, schema + generate modes, with/without frame.
Currently blocked on the workspace spend limit.
