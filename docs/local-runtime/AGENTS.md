# AGENT.md — Local GNSIS Runtime Compatibility & Desktop Control

## Mission

Prove whether the current GNSIS/Gander realtime stack can move from the existing remote-GPU deployment to a **local-first desktop runtime** without losing the product behavior we now care about most:

> GNSIS continuously sees and hears the user, controls the user's actual computer and logged-in browser, adapts across unfamiliar interfaces, verifies outcomes visually, and speaks only when conversation is useful.

The target is not merely "the model starts locally." The target is an end-to-end local desktop experience that preserves the current GNSIS architecture and behavior.

This task must determine:

1. whether the current GNSIS Thinker/Gander speech stack can run through MiniCPM-o 4.5's local runtimes;
2. whether our adapted weights can be converted/loaded into `llama.cpp-omni` or another upstream-supported local backend;
3. exactly what breaks if they cannot;
4. the smallest safe compatibility layer needed to preserve current GNSIS behavior;
5. whether the same architecture works on both macOS and Windows;
6. what the realistic memory/latency floor is for each platform;
7. whether local browser/computer control can use the user's existing environment rather than an isolated browser.

Do not begin by inventing a new runtime, retraining a model, or adding another generic LLM. Reuse existing upstream local inference paths first.

---

## Product priority

This is the highest-priority MVP experience:

> **Can I tell GNSIS to do normal things on my computer, and can it fluidly figure out how to finish them without me describing every click?**

Examples:

- "Move the screenshots I just took into a folder called References."
- "Rename that folder."
- "Play my Release Radar."
- "Open the GNSIS backend and start it."
- "Go to Railway and check what happened to the latest deployment."
- "Find the PDF I downloaded earlier and open it."
- "Go back to that website and download my invoice."

The desired loop is:

```text
HEAR
  ↓
UNDERSTAND
  ↓
SEE
  ↓
ACT
  ↓
SEE
  ↓
ADAPT IF NECESSARY
  ↓
COMPLETE
  ↓
SPEAK ONLY IF USEFUL
```

Do not optimize for making another coding agent. Computer use is the first product experience; developer delegation comes after it.

---

## Baseline architecture

Before changing anything, verify that `main` contains the current architecture:

- Omni-SimpleMem recall surface;
- shared causal timeline;
- delivery gating / output epochs / playback ACK truth;
- model-agnostic `RealtimeProvider` contract;
- desktop Host;
- HarnessDaemon / ACP bridge.

Expected chain:

```text
GNSIS Desktop Host
        │
        ▼
versioned host protocol
        │
        ▼
GNSIS daemon/runtime
        │
        ├── RealtimeProvider
        ├── SessionTimeline
        ├── DeliveryGate
        ├── Omni-SimpleMem
        └── HarnessBridge
                 │
                 ▼
          local execution
```

### Mandatory preflight

Before implementation:

1. confirm the desktop Host files are actually present on `main`;
2. specifically verify `desktop/` exists and the Host/daemon `host.event` seam is present;
3. if the desktop Host is absent or its `host.event` seam is missing, stop implementation and report that the desktop baseline is incomplete;
4. do not silently re-create the desktop Host in this task;
5. verify the current `RealtimeProvider` interface and use it rather than wiring local inference directly into the desktop shell.

The desktop shell must remain replaceable. Electron/Tauri decisions must not leak into model semantics.

---

## Local-first product rule

Local is the default.

The intended product boundary is:

```text
DEFAULT — LOCAL
────────────────────────────
realtime perception
reasoning
speech
personal memory
screen understanding
browser control
filesystem control
app control
local task history

OPTIONAL CLOUD
────────────────────────────
sync across devices
backup
larger/heavier models
remote long-running jobs
remote sandboxes
team/shared memory
burst compute
```

Cloud must be an optional capability, not a requirement for the personal desktop experience.

If a component currently assumes remote storage or compute, make that dependency explicit and identify the local replacement. Do not silently preserve remote dependencies.

---

## Critical distinction: local control vs local inference

Treat these separately.

### Local control

Must run on the user's computer:

- desktop Host;
- Harness daemon;
- filesystem operations;
- process/shell operations;
- native desktop control;
- browser control;
- browser profile/session access;
- local memory store by default.

### Local inference

The target for this task is to determine whether these can also run locally:

- visual perception;
- realtime reasoning;
- audio understanding;
- speech generation;
- the adapted GNSIS/Gander model path.

Do not claim "fully local" if screen/audio is still sent to a remote model server.

---

## Upstream local-runtime facts to verify first

Do not rely only on this file. Re-check upstream before implementation.

### MiniCPM-o 4.5

Upstream currently publishes approximately:

- full MiniCPM-o 4.5: ~19 GB memory;
- GGUF: ~10 GB;
- AWQ: ~11 GB.

Upstream also advertises local low-latency/full-duplex use on Mac.

References:

- https://github.com/OpenBMB/MiniCPM-V
- https://github.com/Mars-Yuan/MiniCPM-o
- https://github.com/OpenBMB/MiniCPM-o-Demo

### llama.cpp-omni

The current MiniCPM-o 4.5 local C/C++ path includes separate GGUF components for:

- LLM;
- audio;
- vision;
- TTS;
- projector;
- token2wav pieces.

Reference:

- https://github.com/tc-mb/llama.cpp-omni

Do not assume one GGUF file contains the whole realtime pipeline.

### Platform support

Verify current upstream support, do not infer it.

Expected investigation:

#### macOS

Preferred first path:

- Apple Silicon;
- Metal;
- official/local MiniCPM-o 4.5 path;
- GGUF / llama.cpp-omni.

#### Windows

Treat Windows as a first-class target, not a later port.

Investigate at least:

- NVIDIA CUDA path;
- official MiniCPM-o desktop installer/runtime path;
- llama.cpp-omni Windows support;
- upstream llama.cpp CUDA/Vulkan support;
- whether full-duplex vision + audio + TTS has feature parity with macOS.

Do not state that Windows is supported merely because llama.cpp builds on Windows. The acceptance requirement is the GNSIS realtime behavior, not compilation.

---

## Primary research question

Answer this with evidence:

> **Can our current GNSIS Thinker + Gander speech components be loaded, converted, or adapted into MiniCPM-o 4.5's local inference path without losing the behavior we added?**

This is the core of the task.

Do not begin custom quantization until this is answered.

---

## Inventory our current adaptation

Before touching model files, document exactly what is custom in GNSIS relative to stock MiniCPM-o 4.5.

At minimum inspect:

- current GNSIS Thinker checkpoint;
- base MiniCPM-o checkpoint;
- Gander Talker checkpoint;
- Token2wav assets;
- reference audio contract;
- text/speech token-unit contract;
- detached Talker behavior;
- interruption behavior;
- screen-frame path;
- context/window behavior;
- task/tool-call behavior;
- any fine-tuned tensors;
- any custom tokenizer/token IDs;
- any custom model config;
- any model-side assumptions in `runtime/minicpm_ft`;
- any CUDA-only code paths;
- any logic that exists in runtime orchestration rather than weights.

Produce a compatibility matrix:

| Component | Stock MiniCPM-o local path | GNSIS customization | Convertible? | Runtime-only? | Risk |
|---|---|---|---|---|---|
| Thinker | | | | | |
| Vision | | | | | |
| Audio input | | | | | |
| Talker | | | | | |
| Token2wav | | | | | |
| Tool calls | | | | | |
| Interruptions | | | | | |
| Context/memory | | | | | |

Do not assume every behavior must be embedded in the local model. Some GNSIS behavior belongs above the model in the shared timeline, delivery gate, Harness, or Host.

---

## Weight compatibility investigation

Determine exactly what our current checkpoints contain.

### Thinker

Inspect:

- tensor names;
- shapes;
- config;
- tokenizer;
- base-model lineage;
- whether the checkpoint is a full checkpoint, delta, adapter, or trainable-tensors-only artifact;
- whether `tts.*` exists;
- whether vision/audio projectors differ from stock;
- whether conversion tools recognize the architecture.

Compare against:

- stock MiniCPM-o 4.5;
- official GGUF conversion expectations;
- llama.cpp-omni model loader expectations.

### Talker

Determine:

- whether our Gander Talker can be represented by the upstream local TTS GGUF format;
- whether it depends on tensors or runtime behavior not supported by llama.cpp-omni;
- whether Token2wav assets are compatible;
- whether our reference-audio behavior maps to upstream `--ref-audio`;
- whether detached-Talker semantics matter once the entire pipeline is local.

Do not throw away our current voice behavior merely to get a successful local load.

### Conversion proof

If conversion appears possible:

1. create a reproducible conversion script;
2. never mutate original model artifacts;
3. record source SHA/checkpoint identity;
4. record converted artifact hashes;
5. verify tensor coverage;
6. fail loudly on missing/unmapped tensors;
7. produce a conversion report.

A "conversion succeeded" message is not proof. All expected model components must be accounted for.

---

## Runtime strategy

Prefer the smallest compatibility layer.

Desired architecture:

```text
GNSIS
  │
  ▼
RealtimeProvider
  │
  ├── RemoteGanderProvider      existing fallback
  │
  └── LocalGNSISProvider       new
          │
          ▼
    local MiniCPM-o runtime
          │
     Metal / CUDA
```

The rest of GNSIS should not care where inference runs.

Do not put MiniCPM/llama.cpp-specific concepts into:

- Desktop Host;
- SessionTimeline;
- DeliveryGate;
- HarnessBridge;
- memory interfaces.

The new local provider should normalize into the existing `ProviderEvent` model.

---

## Required behavior parity

Local inference is acceptable only if it preserves the product-critical behavior.

Test:

### Perception

- microphone input;
- screen input;
- camera input where supported;
- ongoing visual updates;
- source switching;
- timestamps sufficient for the shared timeline.

### Conversation

- full-duplex interaction;
- interruption;
- cancellation of stale output;
- no stale speech after an interruption;
- playback ACK integration;
- selective delivery / no "speak every event" regression.

### Speech

- usable voice quality;
- speech generation starts promptly;
- no runaway backlog;
- no major loss of pronunciation/expressiveness relative to the accepted GNSIS baseline;
- reference-audio behavior if required.

### Reasoning

- current GNSIS/Gander behavior on representative realtime tasks;
- tool/delegation intent still works;
- no silent loss of tool-call semantics.

### Memory

- local Omni-SimpleMem path works;
- no mandatory S3 dependency for personal desktop mode;
- local storage is the default;
- cloud sync/archive may be optional.

---

## Desktop-control objective

The local model work is in service of actual computer control.

The desired control architecture is:

```text
Gander/GNSIS
persistent perception
       │
       ▼
current world state
       │
       ▼
GNSIS decision
       │
       ▼
local executor
   ┌───────┼───────────┐
   │       │           │
files    browser     desktop
   │       │           │
native   OBU/local   UI driver
           browser
   │       │           │
   └───────┼───────────┘
           ▼
        Gander
   verifies outcome
```

Gander should remain the persistent eyes of GNSIS.

Do not create a second competing visual brain by default.

---

## Existing Chrome is a priority

Investigate and prototype an `open-browser-use`-style integration that can control the user's existing browser/profile/session rather than launching a disposable isolated browser.

Desired behavior:

```text
GNSIS
  │
  ▼
local browser-control bridge
  │
  ▼
existing Chrome
  │
  ├── current tabs
  ├── cookies
  ├── existing logins
  └── user's active session
```

Research reference:

- https://github.com/open-browser-use/open-browser-use

Requirements:

- remain local;
- do not copy raw browser credentials into model context;
- use the user's active browser session;
- expose actions/results through the GNSIS timeline;
- Gander observes/verifies the result;
- browser navigation must not force narration of every click.

Do not make isolated Chromium the default if controlling the user's existing browser is viable.

---

## OpenHands role

OpenHands is an optional open-ended executor/navigation fallback, not the primary identity or visual brain.

Desired use:

```text
known/simple task
      │
direct local primitive

unfamiliar environment
      │
OpenHands/open-ended navigator
      │
existing browser/local executor
```

GNSIS owns:

- user relationship;
- intent;
- perception;
- session;
- memory;
- speech policy;
- shared timeline;
- task delivery.

OpenHands may help determine how to navigate an unfamiliar environment.

Do not route every action through OpenHands.

---

## Speech policy

All execution events may enter the timeline.

Very few should become speech.

Preserve the delivery-gating principle:

```text
ALL EVENTS
    │
    ▼
SessionTimeline
    │
    ├── audit/state
    │
    └── speech policy
           │
        useful?
       /      \
     yes       no
      │         │
    speak     silent
```

The system should generally speak for:

- useful acknowledgement;
- blocker/ambiguity;
- materially unexpected result;
- meaningful milestone on a long task;
- completion when useful.

It should not narrate:

- every click;
- every file move;
- every DOM inspection;
- every tool call;
- every shell command.

A 20-step task may produce two spoken utterances.

---

## macOS acceptance

Test on a real supported Mac.

Record:

- chip;
- unified memory;
- macOS version;
- model format;
- model disk size;
- peak unified memory;
- steady-state memory;
- swap;
- first usable session startup;
- first audio latency;
- interrupt latency;
- visual-frame throughput;
- tokens/sec where meaningful;
- real-time factor for TTS;
- CPU/GPU utilization;
- 30-minute stability.

Test at minimum:

1. stock MiniCPM-o 4.5 local runtime;
2. stock GGUF runtime;
3. current GNSIS adaptation if convertible;
4. the best practical local configuration.

If the machine cannot run full weights comfortably, test GGUF before designing a custom quantization scheme.

---

## Windows acceptance

Windows is not optional.

Test a real Windows machine if available; otherwise produce exact reproducible instructions and CI/build proof, clearly labeling hardware runtime proof as pending.

Record:

- Windows version;
- CPU;
- RAM;
- GPU;
- VRAM;
- driver/runtime versions;
- backend: CUDA / Vulkan / CPU fallback;
- same latency/memory/stability metrics as macOS.

At minimum prove:

- build/install;
- model load;
- audio input;
- visual input;
- spoken output;
- interrupt;
- shared timeline integration;
- local Harness;
- local browser control.

If Windows full-duplex support differs from Mac, document the gap exactly.

Do not mask missing functionality behind "Windows supported."

---

## Memory targets

Do not confuse model file size with runtime memory.

For each configuration record separately:

- model files on disk;
- weights resident in memory;
- KV/cache memory;
- vision/audio/TTS memory;
- GNSIS daemon memory;
- Desktop Host memory;
- SimpleMem memory;
- browser memory;
- total system pressure.

The personal desktop must remain usable while Chrome and normal user applications are open.

Do not optimize for a benchmark that consumes nearly all system memory and makes the desktop unusable.

---

## Performance targets

Use measurements, not invented claims.

Compare local against the current remote baseline.

Track at minimum:

- cold app launch;
- model load;
- warm session start;
- user speech end → first response audio;
- visual change → useful model awareness;
- interruption → stale playback stopped;
- task request → first local action;
- browser action round-trip;
- end-to-end completion on real desktop tasks.

A local architecture that is private but unusably slow is not accepted.

---

## Test scenarios

Create an end-to-end acceptance suite around user goals, not only APIs.

### Filesystem

- "Move the screenshots I just took into a new folder called References."
- "Rename that folder."
- "Find the PDF I downloaded earlier and open it."

### Apps

- "Open Spotify and play my Release Radar."
- "Open the GNSIS backend."

### Browser

- "Go back to the page I was looking at."
- "Download my invoice."
- "Open Railway and check the latest deployment."

### Long/multi-step

- "Clean up my Downloads folder."
- "Open the backend, start it, and tell me only if something goes wrong."

For every scenario record:

- user request;
- plan/actions;
- timeline events;
- spoken utterances;
- completion evidence;
- recovery behavior;
- total latency.

Success means GNSIS completes the goal without the user enumerating clicks.

---

## Implementation phases

### Phase 0 — architecture and upstream verification

No product code changes yet.

Deliver:

- current GNSIS customization inventory;
- upstream MiniCPM-o 4.5 local-runtime verification;
- llama.cpp-omni capability matrix;
- macOS vs Windows matrix;
- current model artifact inventory;
- compatibility risks.

### Phase 1 — stock local runtime proof

Run stock MiniCPM-o 4.5 locally first.

Prove:

- audio;
- vision;
- speech;
- full/half duplex behavior;
- interruption;
- resource use.

This establishes whether upstream local runtime itself meets our baseline.

### Phase 2 — GNSIS checkpoint compatibility

Attempt conversion/loading of our current Thinker/Talker.

Deliver either:

A. a reproducible working conversion;

or

B. an exact incompatibility report listing unsupported tensors/config/runtime semantics.

Do not use "not supported" without identifying the specific boundary.

### Phase 3 — LocalGNSISProvider

Implement the smallest provider adapter behind `RealtimeProvider`.

No desktop-specific inference logic.

Preserve shared timeline and delivery semantics.

### Phase 4 — local memory default

Make personal desktop memory local by default.

Cloud archive/sync must be opt-in/configurable.

### Phase 5 — local computer/browser control

Wire:

- filesystem;
- shell/process;
- native desktop control;
- existing-browser control;
- Gander verification loop.

### Phase 6 — platform proof

Benchmark and validate macOS and Windows independently.

### Phase 7 — fallback strategy

Keep the current remote provider as an explicit fallback for:

- unsupported hardware;
- larger-model mode;
- heavy workloads.

Fallback must not be silently selected.

The UI/runtime must know whether a session is local or remote.

---

## Decision tree if conversion fails

If our GNSIS weights cannot run through llama.cpp-omni:

1. identify whether the blocker is weights, architecture, tokenizer, TTS, vision/audio projector, or runtime semantics;
2. determine whether the missing behavior lives in weights or orchestration;
3. test whether stock local MiniCPM-o plus GNSIS runtime-level behavior preserves the experience;
4. only then evaluate a targeted local fine-tune/export;
5. only then evaluate custom quantization/runtime work.

Do not immediately retrain the entire model.

Do not immediately replace Gander.

Do not introduce GPT/Claude as the realtime front brain.

---

## What not to do

Do not:

- replace GNSIS with OpenHands;
- turn the MVP into a coding agent;
- require cloud inference for ordinary desktop control;
- require cloud memory for personal use;
- create a second unrelated visual reasoning loop when Gander can observe the environment;
- narrate every action;
- create a giant fixed list of app-specific tools;
- force browser tasks into an isolated browser by default;
- hand-roll a VM/sandbox platform for this MVP;
- custom-quantize before testing official GGUF/AWQ/local paths;
- claim Windows parity without testing it;
- modify production Modal deployment just to prove local compatibility.

---

## Required deliverables

The PR implementing this task must include:

1. **Compatibility report**
   - GNSIS vs stock MiniCPM-o 4.5;
   - exact custom tensors/behavior;
   - llama.cpp-omni compatibility;
   - conversion outcome.

2. **Platform report**
   - macOS;
   - Windows;
   - backends and hardware requirements;
   - known gaps.

3. **Local runtime**
   - reproducible install/start command;
   - no hidden cloud dependency.

4. **LocalGNSISProvider**
   - behind existing `RealtimeProvider`;
   - tests.

5. **Memory configuration**
   - local default;
   - optional cloud sync/archive.

6. **Desktop/browser control proof**
   - existing Chrome/session path where viable;
   - at least one unfamiliar-site navigation case.

7. **Telemetry**
   - memory;
   - latency;
   - CPU/GPU;
   - startup;
   - interruption;
   - 30-minute stability.

8. **Acceptance results**
   - real user-goal scenarios;
   - timeline evidence;
   - spoken-output evidence.

9. **Fallback**
   - existing remote runtime remains available but is not required for supported local hardware.

---

## Required final report format

End with a concise report:

### Result

- Local GNSIS on Mac: PASS / PARTIAL / FAIL
- Local GNSIS on Windows: PASS / PARTIAL / FAIL
- Current GNSIS weights convertible: YES / PARTIAL / NO
- Full-duplex preserved: YES / PARTIAL / NO
- Gander visual behavior preserved: YES / PARTIAL / NO
- Gander/GNSIS speech preserved: YES / PARTIAL / NO
- Existing Chrome control: PASS / PARTIAL / FAIL
- Local memory default: PASS / PARTIAL / FAIL

### Recommended default

State the exact default local runtime for macOS and Windows.

### Remaining blockers

List only concrete blockers with evidence.

### PRs

Link every implementation PR.

---

## Definition of done

This task is done when we can truthfully demonstrate, on supported hardware:

> **GNSIS runs locally by default, continuously sees/hears the user's environment, controls the user's real desktop and logged-in browser, can recover through unfamiliar interfaces, keeps personal memory local by default, and speaks selectively — while the remote GPU runtime is optional rather than required.**

Do not optimize the definition of done down to "the model loaded."
