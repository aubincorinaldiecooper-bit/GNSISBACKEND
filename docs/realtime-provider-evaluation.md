# Foreground provider evaluation: Thinker baseline vs Realtime-Venus

The realtime runtime now selects its foreground model with one key:

```yaml
realtime:
  provider: thinker        # default and rollback path
  # provider: venus
  # venus_url: http://venus-host:8000
  # venus_timeout_sec: 30
```

Both models sit behind the same `RealtimeProvider` / `RealtimeSession` contract
(`runtime/gnsis_runtime/gnsis_runtime/realtime_provider.py`):

| provider | adapter | what runs where |
| --- | --- | --- |
| `thinker` | `providers/thinker.py` | in-process Thinker on GPU 0, detached Talker + Token2wav on GPU 1 (the current `gnsis-voice` deployment) |
| `venus` | `providers/venus.py` | thin HTTP client of the upstream Realtime-Venus `demos/model` server; weights live on that server |

`realtime.provider` is independent of `worker.provider`: the first picks the
model that sees and hears, the second picks the background action layer.

## What is swappable today

- `gnsis-realtime-bench` drives either provider through identical
  `open_session / push_video_frame / push_audio / next_event / close` calls
  with the same paced input and the same clock, and writes one comparable
  report per run (`runtime/gnsis_runtime/gnsis_runtime/realtime_bench.py`).
- `gnsis-serve --check-config` reports the selected provider and validates a
  Venus URL at preflight.
- `gnsis-serve` serves whichever provider the config names on the desktop
  Host's own sockets through one app, `online_duplex`. `realtime.provider`
  only decides what sits behind the `ForegroundSession` seam
  (`runtime/gnsis_runtime/gnsis_runtime/foreground_session.py`): the
  in-process Thinker/Talker bundle, or a native provider's remote session
  (no Thinker is loaded). Everything above the seam is the same object for
  both: `/ws/duplex` and `/ws/screen`, startup buffering, reconnect grace
  and resume tokens, session expiry, capability negotiation, the bounded
  screen history, `TaskToolsRealtimeCoordinator` with the task-tools
  Gateway, harness bridge and durable session memory, the session timeline,
  the delivery gate and playback acknowledgements, interruption/cancel and
  teardown. The Host does not learn which model it is talking to.
- Native providers additionally receive playback acknowledgements and
  cancellations from the shared path, so they can keep their own output
  epochs honest; tool responses, runtime events, memory episodes and the task
  slate reach them through their control channel.
- `tests/test_live_provider_parity.py` drives the same client against the
  app with `thinker` and `venus` behind the seam and asserts the same
  lifecycle contract, then checks the native path end to end with a scripted
  provider: audio with capture timing, screen frames, speech output and
  interruption, correlated and replay-safe tool calls, and coordinator /
  timeline visibility of model text.
- Each provider is opened with its own prompt
  (`docs/realtime-system-prompt.md`): the Thinker with the prompt it was
  fine-tuned on, a native model with the same guidance minus the unit
  protocol. `realtime.system_prompt` overrides either.

## Matched test plan (not yet run)

One provider per run, never both on the same GPU at once. Every run is an
ephemeral GPU container stopped as soon as its report is written; the
deployed `gnsis-voice` app is untouched.

### Inputs

A fixed scenario set, recorded once and reused for every run:

| scenario | audio | frames | what it exercises |
| --- | --- | --- | --- |
| S1 plain turn | question, silence | none | time to first audio, answer completeness |
| S2 screen question | "what is on the screen right now?" | 1 fps browser capture | audiovisual grounding |
| S3 barge-in | question, then "wait, stop" 1.5 s into the answer | none | cancel latency, stale audio after the interruption |
| S4 backchannel | question, then "mhm" during the answer | none | does the answer continue |
| S5 screen change while speaking | question about the page, page navigates mid-answer | 1 fps | does the answer follow the new screen |
| S6 long session | 10 minutes of mixed turns | 1 fps | stability, memory growth |

Audio is mono 16 kHz 16-bit WAV; frames are a directory of JPEGs pushed in
name order at `--fps`.

### Runs

```bash
# Thinker baseline: the production config, two GPUs as deployed.
gnsis-realtime-bench --config runtime/configs/gnsis-voice.yaml \
  --audio S1.wav --out out/thinker/S1.json

# Venus: one GPU hosting the upstream server, this runtime as its client.
gnsis-realtime-bench --config runtime/configs/gnsis-venus-bench.yaml \
  --venus-url http://127.0.0.1:8000 --audio S1.wav --out out/venus/S1.json
```

Each scenario is run three times per provider; the median is reported.

### Measured per run

From the bench report:

- `open_ms` — session open latency;
- `first_audio_ms` relative to the question's end — time to first audio;
- `events_by_kind`, `audio_pcm16_bytes` — did the model answer, and how much;
- for S3, time from the interruption's capture offset to the last audio event
  of the superseded epoch, and whether any audio of that epoch arrives after
  it — barge-in latency and stale output;
- for S4, whether audio continues through the backchannel;
- `stop_reason` and `error` — stability.

From the container:

- GPU type and count, peak memory per GPU, steady-state utilization;
- cold start to first accepted session;
- GPU-seconds per run, converted to cost at the provider's list price.

Speech quality and grounding correctness are scored by listening to the
recorded output (saved from the report's audio events) against the scenario
script; that is the one manual step.

### Decision rule

Venus becomes the default foreground candidate only if, on these runs, it is
no worse than the Thinker on barge-in latency and stale-output count, is at
least as good on time to first audio and grounding, and runs on one GPU at a
cost per active minute no higher than the two-GPU baseline. Otherwise the
Thinker stays the default and the provider seam stays in place for the next
challenger. Published benchmark numbers do not count toward this decision.
