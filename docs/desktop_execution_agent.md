# Desktop Execution Agent — Electron Now, Replaceable Host Later

## Mission

Build the GNSIS desktop Host now using Electron, with the Qwen Live Harness Host as the primary implementation reference/reuse path.

The priority is to ship a strong realtime desktop experience without coupling GNSIS itself to Electron.

This is an implementation task. Do not turn it into another open-ended architecture study and do not migrate to Tauri in this run.

Use `docs/desktop_chassis_decision.md` as the architecture decision record and source of truth for the desktop boundary.

---

## Locked decisions

### 1. Stay on Electron for this implementation

Use Electron for the current GNSIS desktop Host.

The reason is practical: Qwen Live Harness Host already contains working patterns for the desktop plumbing GNSIS needs, including:

- microphone capture;
- camera and screen capture;
- macOS permission handling;
- streaming audio playback;
- playback-start and playback-complete acknowledgements;
- output/call epochs;
- daemon lifecycle and reconnect;
- device switching;
- native screenshots;
- renderer isolation;
- desktop packaging/signing patterns.

Reuse/adapt these pieces where practical instead of rebuilding them.

Do not import Qwen branding, product identity, or the assumption that Qwen Omni is the only realtime provider.

### 2. ClickyX is a reference, not a chassis migration

Use ClickyX as a reference for useful desktop/chassis ideas, especially:

- tray/application behavior;
- overlays;
- local bridge patterns;
- computer-use integration;
- cross-platform structure;
- Tauri implementation patterns that may be useful later.

Do not switch GNSIS to Tauri in this run.

### 3. Electron is only the Host

The desktop shell is not GNSIS.

Electron may own:

- windows;
- tray/menu;
- native permissions;
- microphone/camera/screen devices;
- capture/resampling;
- speaker playback;
- native screenshots;
- keyboard/global shortcuts;
- desktop notifications;
- install/update lifecycle;
- visible desktop UI.

Electron must not own:

- realtime model semantics;
- GNSIS reasoning;
- memory;
- task orchestration;
- ACP/backend-agent logic;
- delivery-gating policy;
- durable task state;
- conversation truth;
- provider selection.

Those belong behind the GNSIS daemon/runtime boundary.

### 4. Preserve a chassis-neutral Host <-> daemon protocol

The daemon must not know whether the desktop Host is Electron, Tauri, a browser, or another native client.

Use neutral GNSIS protocol events such as:

- `host.ready`
- `host.disconnected`
- `call.started`
- `call.ended`
- `audio.frame`
- `screen.frame`
- `video.frame`
- `device.changed`
- `permission.changed`
- `playback.started`
- `playback.completed`
- `playback.cancelled`

Every event must carry the IDs/timestamps needed by the shared GNSIS causal timeline.

Version the Host/daemon protocol from the start. Prefer capability/version negotiation over silently breaking older Hosts.

### 5. Keep chassis-specific code behind small adapters

At minimum isolate:

- audio capture;
- screen/camera capture;
- playback;
- permissions;
- native screenshots;
- global shortcuts;
- desktop notifications.

Do not create a giant abstraction framework. The goal is a clean seam, not speculative infrastructure.

---

## Performance methodology — priority requirement

Do not invent hard performance thresholds before the real GNSIS desktop implementation exists.

The correct process is:

1. build a representative Electron/Qwen-Host-derived GNSIS Host;
2. measure the real baseline;
3. separate Host overhead from daemon/network/model latency;
4. compare the baseline with established desktop/realtime application norms where useful;
5. define elastic performance bands;
6. optimize obvious problems;
7. re-measure;
8. use persistent user-visible misses to decide whether a chassis review is justified.

### Use three bands, not binary tripwires

For every important metric define:

**Target / healthy**
- where normal operation should usually sit.

**Warning / acceptable**
- still shippable;
- monitor;
- investigate;
- optimize when worthwhile.

**Migration-review**
- persistent behavior in this band after a focused Electron optimization pass is evidence for comparing Tauri.

Do not create rules such as:

`501 MB = failure`

or:

`one missed benchmark = rewrite in Tauri`.

The bands should include reasonable engineering headroom.

Where appropriate, use p50/p95/p99 rather than averages alone. Realtime experience is often dominated by tail latency.

### Document the baseline environment

Every accepted performance baseline must state:

- machine/model;
- CPU architecture;
- RAM;
- OS/version;
- Electron/Host build;
- daemon build;
- capture source and resolution;
- configured frame rate;
- microphone/output device;
- whether Bluetooth was involved;
- session duration;
- measurement method;
- whether the realtime provider was local or remote where relevant.

Future Electron/Tauri comparisons must use equivalent workloads.

---

## Metrics to establish

### Launch and recovery

Measure and establish bands for:

- cold app launch -> usable UI;
- warm app launch -> usable UI;
- Host -> daemon initial connection;
- daemon reconnect once endpoint is available;
- recovery after daemon restart;
- recovery after ordinary network interruption;
- sleep/wake recovery.

Correctness is more important than a tiny latency difference:

- no stale audio replay;
- no obsolete output epoch revived;
- no duplicated background result;
- no duplicate task delivery.

### Audio path

Measure Host-only contribution separately from model/network inference:

- mic capture -> daemon receive;
- audio chunk received by Host -> playback scheduled;
- playback scheduled -> actual playback-start ACK;
- cancellation request -> actual playback stop;
- playback-complete accuracy;
- underruns;
- gaps;
- duplicated chunks;
- stale chunks rejected;
- audio drift over long sessions.

**Playback ACKs are authoritative.**

Audio generated by a model or forwarded to the Host does not mean the person heard it.

The delivery-gating system must use actual `playback.started`, `playback.completed`, and cancellation state.

### Visual path

Measure:

- screen/camera capture rate;
- capture -> daemon receive;
- Host-side frame loss;
- frame ordering;
- timestamp monotonicity;
- source switch time/recovery;
- freeze/restart rate;
- sustained degradation during long sessions.

Tie thresholds to the actual configured capture rate/resolution.

### Resource use

Measure the complete Host process group, not one renderer.

Establish elastic bands for:

- idle CPU;
- active capture CPU;
- microphone processing CPU;
- playback CPU;
- steady-state RSS;
- memory growth during a session;
- memory returned after session end;
- GPU use caused by the Host where applicable;
- battery drain;
- thermal behavior;
- sustained throttling indicators where available.

Battery/thermal behavior is a first-class desktop metric.

A Host that technically meets RAM/CPU limits but makes a laptop hot or materially damages battery life is not a good desktop experience.

### Long-session behavior

Do not approve the desktop Host from a short demo.

Run both short tests and meaningful sustained live sessions.

Use the real product baseline to settle exact durations, but include at least one 30-60+ minute class of run capable of exposing:

- memory creep;
- audio drift;
- capture degradation;
- growing playback lag;
- reconnect-state corruption;
- stale task/output state;
- resource leaks;
- thermal throttling;
- rising CPU;
- device handles not being released.

---

## Reliability matrix

Test these explicitly:

1. microphone permission grant;
2. microphone revoke and re-grant;
3. screen-recording permission grant;
4. screen-recording revoke and re-grant;
5. camera permission grant;
6. camera revoke and re-grant;
7. screen-source switching;
8. camera-source switching;
9. microphone device switching;
10. output device switching where supported;
11. Bluetooth device behavior where applicable;
12. sleep/wake;
13. daemon restart;
14. ordinary network interruption;
15. realtime-provider disconnect/reconnect;
16. output cancellation during playback;
17. repeated call start/end cycles;
18. Host reconnect while a background task exists;
19. stale output arriving after a newer epoch;
20. sustained full-duplex session.

A single defect is not automatically a chassis problem.

Fix normal bugs normally.

A Tauri review is justified only when the failure is structural, persistent, or tied materially to the Electron chassis after a bounded optimization pass.

---

## Failure/recovery requirements

Failure and recovery are part of performance.

The Host must fail safely when:

- daemon disappears;
- realtime provider disappears;
- device disappears;
- permission is revoked;
- capture source closes;
- audio output fails;
- laptop sleeps;
- connection returns.

Do not silently reconnect into stale conversation state.

Call/output epochs must prevent old work from becoming current after recovery.

Background task state belongs to the daemon/task system and must survive ordinary Host reconnects where the rest of the architecture supports it.

---

## Tauri review rule

Do not migrate because:

- Tauri sounds more native;
- Rust is preferred;
- Electron has a reputation for being heavy;
- ClickyX uses Tauri;
- Tauri has a smaller binary;
- one microbenchmark wins;
- one metric briefly enters the warning band.

Open a formal Tauri comparison when either:

### Hard blocker

Electron cannot reasonably satisfy a required capability such as:

- capture/permission correctness on a required OS;
- required Windows/Linux support;
- full-duplex playback/cancellation semantics;
- accurate playback ACKs/output epochs;
- required security/sandboxing behavior.

### Persistent measured problem

After a focused Electron optimization pass, the Host remains in migration-review territory for a combination of metrics that creates a material:

- user-facing latency problem;
- reliability problem;
- battery/thermal problem;
- memory/CPU problem;
- capture problem;
- platform problem.

There is no magic count of failed metrics.

Judge the pattern and severity of evidence.

---

## If Tauri review is triggered

Build a small Tauri/ClickyX-inspired Host prototype against the **same GNSIS daemon protocol**.

Do not modify the core architecture simply to make the Tauri prototype look better.

Measure the same workload, hardware, daemon, provider, capture configuration, and test scenarios.

Compare:

- latency;
- playback/cancellation correctness;
- capture reliability;
- CPU;
- memory;
- battery/thermals;
- startup/reconnect;
- cross-platform complexity;
- implementation/migration risk.

A migration must materially solve the actual problem that triggered the review.

Do not require an arbitrary universal percentage improvement.

The improvement must matter in the real GNSIS experience.

---

## Privacy and diagnostics

Routine desktop telemetry should record metadata and timing, not raw user content.

Do not routinely persist:

- microphone audio;
- screen pixels;
- camera video.

If raw media is required for a debugging experiment:

- make capture explicit;
- time-bound it;
- document retention;
- make removal possible;
- never log secrets alongside it.

Do not log:

- API keys;
- daemon credentials;
- auth tokens;
- private memory contents unless a specific sanitized diagnostic requires them.

---

## Required telemetry

Before desktop acceptance, make it possible to reconstruct:

- app launch;
- Host ready;
- daemon connected;
- daemon disconnected/reconnected;
- call started/ended;
- permission request/result;
- device selection/change;
- capture start/stop;
- audio frame captured;
- audio frame received by daemon;
- visual frame captured;
- visual frame received by daemon;
- audio chunk received by Host;
- playback scheduled;
- playback actually started;
- playback actually completed;
- playback cancelled;
- output epoch;
- stale output rejected;
- frame counts/drops;
- Host CPU samples;
- Host memory/RSS samples;
- battery/thermal measurements where the platform exposes useful data.

Telemetry names should line up with the shared GNSIS timeline where possible.

---

## Implementation sequence

### Step 1 — Establish the desktop boundary

Before deep UI work:

- define/version the neutral Host <-> daemon protocol;
- isolate capture/playback/permission adapters;
- preserve output/call epochs;
- preserve playback ACK semantics.

Do not redesign the GNSIS daemon around Electron.

### Step 2 — Reuse the Qwen Host chassis

Evaluate and reuse/adapt the existing Qwen Live Harness Host pieces that save work.

Prefer reuse over reimplementation when behavior is compatible.

Record any pieces that cannot be reused and why.

### Step 3 — GNSIS integration

Connect the Host to GNSIS:

- realtime provider path;
- shared timeline;
- playback/output epochs;
- delivery gating;
- screen/camera input;
- task state displays where required.

The Host should remain agnostic to whether the foreground provider is Gander, Venus, or another future realtime provider.

### Step 4 — Working baseline before optimization

Get a functional end-to-end desktop session.

Do not prematurely optimize individual Electron internals before measuring the complete path.

Record the first baseline.

### Step 5 — Establish elastic SLO/budget bands

Using the measured baseline plus relevant desktop/realtime norms:

- define target/healthy;
- warning/acceptable;
- migration-review.

Explain the reasoning and headroom for each important metric.

These numbers belong in an evidence-backed follow-up/acceptance record, not as arbitrary constants copied from this document.

### Step 6 — Focused optimization

Fix meaningful Electron/Host bottlenecks.

Re-run identical measurements.

Do not tune away correctness for benchmark wins.

### Step 7 — Acceptance

Run:

- short realtime tests;
- failure/recovery matrix;
- sustained live session;
- resource/battery/thermal measurements;
- playback/cancellation tests.

Produce a concise desktop acceptance report with the baseline, optimized measurements, and final bands.

---

## Definition of done

The desktop execution is ready when:

1. GNSIS runs as a real Electron desktop Host.
2. Qwen Host infrastructure has been reused where practical instead of unnecessarily rebuilt.
3. Electron-specific APIs remain at the Host edge.
4. The daemon protocol is neutral and versioned.
5. Mic, screen, camera, and playback operate reliably.
6. Playback-start/completion ACKs are real device facts.
7. Output epochs prevent stale playback.
8. Delivery gating remains correct through the desktop path.
9. Daemon reconnect does not duplicate speech/tasks or revive stale state.
10. Short and sustained sessions pass the reliability matrix.
11. CPU, memory, latency, frame reliability, battery, and thermal baselines are measured.
12. Performance bands are evidence-based and elastic.
13. Warning-band misses create tuning work, not automatic rewrites.
14. Migration-review conditions are documented from real measurements.
15. Tauri remains replaceable behind the same Host/daemon contract if real evidence later justifies it.

The objective is not to prove Electron is perfect.

The objective is to deliver a good GNSIS desktop experience now while preserving a clean, measurable exit if the chassis later becomes the constraint.
