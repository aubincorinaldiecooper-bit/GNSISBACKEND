# AGENT.md — GNSIS Unified Omni Implementation

## Mission

Implement the next GNSIS realtime architecture with the least reinvention possible.

GNSIS is a **general-purpose realtime agent**, not a product optimized around one narrow use case. The system must feel like one continuous intelligence while remaining modular underneath:

- continuously see and hear;
- speak naturally while continuing to perceive;
- be interruptible without awkward Thinker -> Talker seams;
- remember across time and sessions;
- delegate deeper work without freezing the foreground conversation;
- announce background results only when conversationally appropriate;
- allow the foreground realtime model, speech renderer, memory engine, and task backends to be replaced independently.

This is an **implementation brief**, not an open-ended research exercise. Use existing open-source systems directly where they already solve the problem. Do not spend a long cycle recreating Qwen Live Harness, Omni-SimpleMem, or desktop media plumbing before proving a direct reuse path is blocked.

---

## Locked decisions

These are requirements for this run, not optional future work.

### 1. Omni-SimpleMem is required memory infrastructure

Do **not** treat memory as optional or leave GNSIS with the current inert `SimpleMemProvider`.

The existing open-source Omni-SimpleMem integration already exists in Clipit and must be treated as reusable prior art:

- Clipit PR #106/#107 integrated Omni-SimpleMem retrieval.
- PR #109 made Omni-SimpleMem the preferred early retrieval path.
- PR #114 deployed a dedicated hardened Omni-SimpleMem sidecar with durable object storage.
- PR #123 pinned the Transformers contract required for working visual CLIP embeddings.
- PR #126 hardened memory recovery/versioning.

Current reusable Clipit implementation on `main`:

- `tools/simplemem/sidecar.py`
- `tools/simplemem/captions.py`
- `tools/simplemem/requirements.txt`
- `Dockerfile.simplemem`
- `src/services/retrieval/simplemem/client.ts`

Important: this is the **open-source Python Omni-SimpleMem path**, not the SimpleMem MCP server. Keep it that way. Upstream currently exposes multimodal image/audio/video memory through the Python package; MCP is not the intended GNSIS integration boundary.

The Clipit sidecar already calls the unified package in Omni mode, preserves source-time metadata, uses a shared visual/text embedding space, has an authenticated service boundary, and persists completed memories to durable S3-compatible archives while treating local disk as cache. Reuse this implementation rather than designing a new memory engine.

### 2. Durable recall must be live in GNSIS

Current `GNSISBACKEND/main` has two separate memory histories:

1. The coding/service layer has `PostgresMemoryProvider` / CodeMemory with approval-gated provenance.
2. The realtime runtime exposes `HttpMemoryProvider`, but the live Thinker context is not being fed by a real durable multimodal memory producer.
3. `src/gnsis/memory/SimpleMemProvider` is currently a deliberate `NotImplementedError` stub.

That must change.

Omni-SimpleMem becomes the **required general recall service** for GNSIS. Postgres CodeMemory remains useful as the authoritative/auditable store for approved coding intelligence and provenance, but it must not be mistaken for the general multimodal memory system.

Where appropriate, mirror approved CodeMemory records into Omni-SimpleMem as typed semantic memories while preserving the Postgres row/provenance ID as metadata. Postgres remains the audit/source-of-truth for reviewed code intelligence; SimpleMem becomes the common retrieval surface used by GNSIS recall.

### 3. Episodic and semantic memory work in tandem

Implement both.

**Episodic memory** answers: "What happened?"

Examples:
- screen state at a timestamp;
- a warning appearing before a crash;
- what the user said while a specific visual state was present;
- what changed between two observed states.

**Semantic memory** answers: "What do I know?"

Examples:
- a project decision;
- a user preference;
- an accepted architectural rule;
- a recurring failure pattern;
- a verified task result.

Use the same durable memory infrastructure where practical, but keep explicit memory types/namespaces and provenance.

At minimum support:

`episode`, `visual_event`, `audio_event`, `fact`, `decision`, `preference`, `task_result`, `approved_code_intelligence`.

Episodic memories must retain source-time and source references. Semantic memories may be synthesized/compressed from episodes but must retain provenance pointers to the source events that produced them.

### 4. Venus gets first priority as the foreground-model challenger

Current GNSIS production voice architecture uses:

- Gander Thinker on GPU 0;
- detached Gander Talker + Token2wav on GPU 1;
- raw audio/video into the Thinker;
- learned `listen / speak / interrupt / backchannel` decisions;
- detached speech so perception can continue while speech is generated.

Do not protect this architecture because it already works.

Realtime-Venus should be the **first foreground model tested against Gander** because its architecture and reported realtime/full-duplex results make it the most relevant current challenger.

The goal is not "replace Gander because a paper score is higher." The goal is to determine whether native Omni full-duplex produces a materially better GNSIS foreground experience than the current detached Thinker -> Talker arrangement.

Evaluate both under the same GNSIS session/timeline/harness plumbing.

### 5. Native Omni speech is preferred if it wins the realtime evaluation

A native Omni model that perceives, controls turn-taking, produces speech, hears while speaking, and changes course inside one realtime loop is architecturally preferable to a detached Talker **if** it meets GNSIS latency, quality, stability, cost, and control requirements.

Gander's detached Talker remains the working baseline and rollback path until the benchmark is complete.

Do not remove it first.

### 6. Raw audio remains authoritative; ASR is auxiliary

Do not put ASR between the microphone and the foreground realtime intelligence.

The foreground model should receive raw streaming audio so it can use timing, overlap, pauses, prosody, background speech, backchannels, and interruptions.

ASR/transcripts are still useful for:

- durable memory;
- search;
- task delegation;
- external reasoning models;
- logs/telemetry;
- accessibility/UI.

But transcript text must not replace the raw audio stream as the interaction authority.

This is an architectural requirement, not a claim that Gander's audio model has already beaten Venus/Qwen in a benchmark.

### 7. Delivery gating is mandatory

A background task becoming complete must **never** mean "speak immediately."

Implement an explicit result-delivery state machine and make playback acknowledgements part of the truth.

Required lifecycle:

```
running
  -> completed
  -> result_ready
  -> awaiting_delivery
  -> delivering
  -> delivered
```

Also support cancellation/supersession.

A result may move from `awaiting_delivery` to `delivering` only when all required gates are satisfied:

- user is not currently speaking;
- foreground realtime model is not currently speaking;
- prior device playback is not still draining;
- result belongs to the current valid call/session epoch;
- result/task has not been superseded or cancelled;
- permissions/approval state allows delivery;
- there is no higher-priority foreground response pending.

Generation complete is not playback complete.

The desktop/client playback acknowledgement is the authoritative signal that audio actually started/finished.

If the user begins speaking while a background announcement is queued, keep it queued.

If the user meaningfully interrupts during delivery, cancel the stale output epoch. Re-deliver later only if the result is still relevant.

This behavior is required in the implementation, tests, and telemetry. Do not leave it as a TODO.

### 8. Reuse Qwen Live Harness directly before writing another harness

Qwen Live Harness v1.0.0 already contains production-oriented solutions for:

- daemon-level conversation scheduling;
- background task delegation;
- ACP backends;
- Codex adapter;
- Claude Code adapter;
- Gemini CLI ACP;
- Qwen Code / Qwen Serve;
- permission forwarding;
- task lifecycle;
- cancellation;
- foreground/background separation;
- result scheduling;
- proactive monitors;
- memory lifecycle;
- playback/output IDs and acknowledgements.

Its daemon exposes a `BackendAdaptor` boundary. ACP-compatible agents can generally be configured instead of hand-integrated one by one.

The default action is **direct reuse/adaptation of the Qwen Live Harness daemon/ACP subsystem**, not copying its ideas into another custom worker framework.

Only write replacement orchestration code when a concrete incompatibility is demonstrated in code or tests.

### 9. The Qwen Host is now in scope because GNSIS plans a desktop app

Earlier assumptions that "we do not need Qwen Host" no longer apply.

Qwen Host already implements substantial desktop plumbing that GNSIS will otherwise need:

- Electron lifecycle;
- macOS mic/camera/screen permissions;
- 16 kHz PCM microphone capture;
- 24 kHz playback handling;
- camera and screen capture;
- native screenshots;
- output IDs;
- playback-start/completion acknowledgements;
- call epochs;
- daemon reconnect/lifecycle;
- global shortcut;
- renderer sandboxing/context isolation;
- device switching;
- signed/notarized desktop packaging patterns.

Do **not** automatically adopt its product UI or Qwen model dependency.

Do evaluate **forking/adapting Host as the GNSIS desktop shell** before building equivalent device/permission/media infrastructure from scratch.

GNSIS owns branding, product UI, realtime provider choice, memory, and task orchestration identity.

### 10. GNSIS owns one shared causal session timeline

The system needs one ordered event plane that every component reports into.

Do not leave perception, Talker, task workers, memory, and playback as adjacent systems with unrelated state.

At minimum represent:

- mic frame / speech start / speech end;
- camera/screen frame and capture timestamp;
- foreground model input unit;
- foreground model listen/speak/backchannel/interrupt decision;
- response/output epoch;
- speech generation start/chunk/end;
- device playback start/chunk/end/cancel;
- user interruption;
- delegation;
- task progress/completion/cancellation;
- permission request/decision;
- memory episode created;
- memory retrieval;
- background result queued/delivered.

Every event needs:

- session/call ID;
- monotonic sequence;
- source timestamp where available;
- server receive timestamp;
- current call/output epoch where relevant;
- originating component;
- correlation/task/output ID.

This timeline is the coordination boundary that makes underlying components replaceable.

---

## Verified current-state facts

### GNSIS current realtime baseline

`runtime/configs/gnsis-voice.yaml` currently configures:

- MiniCPM-o 4.5 base;
- GNSIS/Gander Thinker;
- Gander detached Talker;
- two CUDA devices;
- raw Omni camera/screen/audio input;
- `generate_audio: true`;
- `worker.provider: none`.

The detached path exists specifically so synthesis does not block perception.

The current runtime already has cancellation/playback protocol pieces worth preserving.

### GNSIS current memory boundaries

`runtime/gnsis_runtime/gnsis_runtime/memory_provider.py` provides an HTTP memory search client.

The realtime runtime also has the `context_memory` / memory-episode mechanism, but current production does not have a real server-side episode producer feeding it.

Do not route durable memory through a browser/client control message as the permanent architecture. Move episode creation/retrieval to server-owned session infrastructure.

### Clipit Omni-SimpleMem implementation

Treat the current Clipit `main` implementation as the reusable reference implementation.

Important existing behavior:

- upstream `simplemem` package;
- `create(mode="omni")`;
- video/image multimodal processing;
- shared CLIP visual/text embedding identity;
- explicit embedding-version checks;
- source-second timeline metadata;
- authenticated sidecar;
- durable S3-compatible archives;
- restore-on-cache-miss;
- checksum validation;
- safe extraction;
- bounded local cache;
- caption health accounting;
- visual embedding compatibility guard.

Do not throw this away and create a weaker SimpleMem integration in GNSIS.

---

## Target architecture

```
                                  GNSIS

                         DESKTOP / WEB CLIENT
                    (Qwen Host-derived candidate)
                                  |
                mic / camera / screen / playback ACKs
                                  |
                                  v
                    +-----------------------------+
                    |   REALTIME PROVIDER LAYER   |
                    |-----------------------------|
                    |  Venus-Omni     candidate   |
                    |  Gander         baseline    |
                    |  future Omni providers      |
                    +--------------+--------------+
                                   |
                           interaction events
                                   |
                                   v
                    +-----------------------------+
                    |  GNSIS CAUSAL TIMELINE      |
                    | session / epochs / playback |
                    +--------+-----------+--------+
                             |           |
                  +----------+           +------------------+
                  v                                         v
          MEMORY PIPELINE                           TASK/AGENT PIPELINE
          |                                         |
          |                                         v
          |                              Qwen Live Harness daemon
          |                                 / ACP subsystem
          |                                         |
     +----+----------------+              +---------+-----------+
     |                     |              |         |           |
     v                     v              v         v           v
 episodic memory      semantic memory   Codex    Claude      Gemini /
     |                     |                                  other
     +----------+----------+
                |
                v
        Omni-SimpleMem
      durable multimodal recall

Approved coding intelligence:
Postgres CodeMemory (audit/source of truth)
        |
        +---- mirror typed memory + provenance pointer ----> Omni-SimpleMem

Background task result
        |
        v
  result_ready
        |
        v
  DELIVERY GATE
  - user silent?
  - foreground response idle?
  - playback drained?
  - current epoch?
  - still relevant?
        |
        v
  foreground realtime model decides/announces naturally
```

---

## Realtime provider contract

Create or formalize one model-agnostic provider contract before deeply modifying Gander or Venus.

Suggested responsibilities:

```python
class RealtimeProvider:
    async def start_session(...)
    async def push_audio(...)
    async def push_video(...)
    async def push_control(...)
    async def events(...)
    async def cancel_output(...)
    async def close_session(...)
```

Provider events should normalize to GNSIS timeline events rather than exposing model-specific wire formats to the rest of the product.

Do not design the interface around Gander-specific Thinker/Talker concepts. Venus/native-Omni providers must fit without pretending to have a detached Talker.

---

## Work plan

### Phase 0 — Source-of-truth audit and branch setup

Timebox this. Do not turn it into a research report.

1. Inspect current GNSIS main.
2. Inspect current Clipit Omni-SimpleMem files listed above.
3. Inspect current Qwen Live Harness daemon + Host boundaries.
4. Inspect Realtime-Venus runnable duplex entry points and model/resource requirements.
5. Record exact upstream commits/revisions/licenses used.
6. Produce a short architecture delta in the PR, then move to code.

Exit condition: concrete file map + implementation branch. No "recommendation-only" deliverable.

---

### Phase 1 — Make Omni-SimpleMem real GNSIS infrastructure

Goal: a GNSIS session can write and recall durable semantic + episodic memories through the existing open-source Omni-SimpleMem implementation.

#### 1A. Reuse the Clipit service

Prefer one of these in order:

1. Extract the existing Clipit Omni-SimpleMem sidecar into a shared reusable package/service consumed by both Clipit and GNSIS.
2. If cross-repo extraction is materially slower, port the hardened service boundary into GNSIS with explicit provenance to the Clipit implementation and tests proving behavior parity.

Do not start a third unrelated memory implementation.

#### 1B. Replace the GNSIS SimpleMem stub

`SimpleMemProvider` must become functional.

Do not make its production behavior `NullMemoryProvider` fallback.

Production should fail health/readiness when required memory is configured but unavailable rather than silently running memoryless.

#### 1C. Wire realtime recall

Implement server-owned memory retrieval for the live session.

The foreground model should be able to receive compact relevant memories without the desktop/browser having to synthesize `memory.episode` messages itself.

Preserve bounded model context. Retrieval should feed only relevant memories.

#### 1D. Wire episodic writes

Build episodes from the shared timeline.

Episodes should include:

- time range;
- text summary;
- visual/audio source references;
- relevant screen/camera frame identifiers;
- transcript slice if available;
- task/output IDs if related;
- confidence/provenance.

Do not archive every raw frame as a semantic memory.

Keep exact media in the media/session store and keep pointers in memory where practical.

#### 1E. Bridge CodeMemory

When reviewed coding intelligence becomes authoritative in Postgres, write/mirror a corresponding typed semantic memory to Omni-SimpleMem containing the Postgres/provenance handle.

Do not delete the Postgres audit path.

#### Acceptance

A new session must be able to ask about something learned/observed in an earlier session and receive the relevant memory without a manual client-side injection.

Tests must cover cross-session recall and namespace isolation.

---

### Phase 2 — Shared causal timeline + mandatory delivery gating

Implement this before adding more custom agents.

#### Timeline

Create one in-process session event bus with durable/structured logging sufficient to reconstruct a run.

A full event-sourcing rewrite is not required. Keep it minimal.

#### Output epochs

Every foreground response/audio stream gets an output epoch/generation ID.

When the user meaningfully interrupts:

1. increment/cancel the active output epoch;
2. stop renderer/model output;
3. issue playback cancellation;
4. reject late chunks from the stale epoch.

#### Delivery scheduler

Implement the required result state machine and gates from the locked decisions section.

Use device playback ACKs, not "audio generated," as the final playback state.

#### Acceptance tests

Cover:

- task finishes while user is speaking -> no announcement;
- task finishes while foreground response is speaking -> queued;
- task finishes while prior audio is still draining -> queued;
- user interrupts background announcement -> stale output cancelled;
- stale completion from an old call epoch -> never played;
- two tasks finish together -> serialized delivery;
- superseded task -> never delivered;
- reconnect -> no duplicated announcement;
- playback ACK lost/timeout -> safe deterministic recovery.

This is mandatory before declaring orchestration complete.

---

### Phase 3 — Adopt Qwen Live Harness daemon/ACP instead of extending the custom worker first

Goal: get real background agents working through an existing adapter layer.

#### Direct reuse target

Start from Qwen Live Harness's daemon and `BackendAdaptor`/ACP code.

Attempt to run at least:

- Codex through its existing ACP adapter;
- Claude Code through its existing ACP adapter.

Do not write fresh Codex/Claude process managers unless direct use is blocked.

#### GNSIS integration

Add the thinnest boundary needed between:

`GNSIS timeline / realtime provider <-> Qwen daemon task APIs`

Do not allow the Qwen daemon to become the product identity. GNSIS owns session IDs, memory, policy, and UI.

Background agent events must be normalized onto the GNSIS timeline.

Qwen result scheduling behavior should be reused where compatible, but final speech delivery still obeys GNSIS delivery gates.

#### Permissions

Preserve explicit approval boundaries.

A backend asking for permission is a state transition, not permission to silently execute.

#### Acceptance

From a live GNSIS conversation:

1. delegate a task to Codex;
2. continue the conversation while Codex works;
3. interrupt/change the task where supported;
4. receive progress;
5. result reaches `result_ready`;
6. result is not spoken until the delivery gate opens;
7. result is announced once;
8. cancellation shuts the worker/session down cleanly.

---

### Phase 4 — Desktop Host reuse evaluation and implementation

Because a GNSIS desktop app is planned, do not postpone this to an unrelated future rewrite.

Evaluate Qwen Host as a fork/adaptation base.

Reuse where practical:

- Electron/native lifecycle;
- permission flows;
- screen/camera capture;
- mic AudioWorklet;
- playback engine;
- output IDs;
- playback ACK protocol;
- call epoch handling;
- daemon discovery/reconnect;
- context isolation/sandboxing;
- device switching;
- native screenshots;
- packaging/signing structure.

Replace:

- Qwen branding/UI;
- Qwen-specific config assumptions;
- direct dependence on Qwen Omni as the only foreground provider.

The Host should talk to the GNSIS coordinator/provider contract.

#### Acceptance

The desktop prototype must:

- capture mic + selected screen/camera;
- display GNSIS state;
- receive streaming audio;
- ACK actual playback start/end;
- cancel playback on a new output epoch;
- survive daemon reconnect without replaying stale audio;
- preserve macOS permission correctness.

If direct Host reuse proves impractical, document the exact incompatible surfaces before writing replacements.

---

### Phase 5 — Venus-first foreground evaluation

Do this as an implementation benchmark, not a literature review.

Add Venus behind the `RealtimeProvider` interface.

Keep Gander available behind the same interface.

#### Test scenarios

At minimum:

1. normal conversational turn;
2. user backchannel ("yeah", "mhm") while assistant speaks;
3. genuine interruption ("wait", correction, new instruction);
4. background speech not addressed to GNSIS;
5. user speaks over the start of assistant audio;
6. screen changes while assistant speaks;
7. question referring to current screen;
8. question referring to an earlier episodic memory;
9. delegation while the live conversation continues;
10. worker result becomes ready during user speech;
11. long session;
12. reconnect.

#### Measure

- time to first meaningful audio;
- barge-in/cancel latency;
- false interruption rate;
- missed interruption rate;
- backchannel continuation;
- foreground perception continuity while speaking;
- ability to change response after new sensory input;
- speech quality/naturalness;
- audiovisual grounding;
- GPU memory;
- steady-state GPU utilization;
- cold start;
- session stability;
- cost per active minute where measurable.

#### Decision rule

Venus gets first testing priority.

If Venus materially improves the realtime experience without unacceptable resource/stability regressions, make it the default foreground candidate.

If it does not, keep Gander as default and retain the provider abstraction.

Do not merge a "Venus is better" conclusion based only on published benchmark numbers.

---

## Speech renderer policy

Prefer native Omni speech when the chosen foreground model provides the best full-duplex behavior.

If an external voice model is later used, treat it as a **renderer**, not the interaction controller.

The realtime provider/GNSIS timeline must still own:

- whether to speak;
- response/output epoch;
- interruption;
- cancellation;
- current playback state;
- whether stale audio is allowed.

External TTS must never independently decide conversation state.

---

## ASR policy

ASR should be enabled as a side channel after the realtime loop is stable.

Use it for:

- transcript history;
- semantic memory text;
- agent delegation;
- search/logging;
- accessibility.

Do not make a transcript prerequisite for realtime turn-taking.

Timestamp transcript segments to the same shared timeline.

---

## Memory policy

### Working memory

Owned by the realtime provider/context window.

Short-lived, immediate.

### Episodic memory

Derived from timeline events and multimodal evidence.

Stored durably through Omni-SimpleMem with temporal/source metadata.

### Semantic memory

Facts, decisions, preferences, task results, learned rules.

Stored durably through Omni-SimpleMem.

### Approved coding intelligence

Authoritative provenance stays in Postgres CodeMemory.

Mirror/index into Omni-SimpleMem for common recall.

### Retrieval

Memory retrieval must return structured provenance, not only prose.

At minimum return:

- memory ID;
- type;
- summary/content;
- confidence/score;
- source session;
- source time range;
- source event/media refs;
- original authoritative record ID where relevant.

---

## What not to do

- Do not use the SimpleMem MCP path for multimodal GNSIS memory.
- Do not leave `SimpleMemProvider` as a production stub.
- Do not silently fall back to no memory.
- Do not create another generic vector-memory system.
- Do not rebuild Qwen's ACP adapters before attempting direct reuse.
- Do not treat task completion as permission to speak.
- Do not infer playback completion from model/TTS completion.
- Do not discard Qwen Host without testing reuse now that a desktop app is planned.
- Do not delete Gander before Venus is benchmarked in the same GNSIS plumbing.
- Do not put ASR in front of raw audio.
- Do not hard-code the core orchestration around Gander's Thinker/Talker topology.
- Do not optimize GNSIS around one narrow use case.
- Do not spend a long R&D cycle producing another architecture report instead of running code.

---

## Telemetry required

Every live acceptance run should make it possible to reconstruct:

- provider/model loaded;
- session/call ID;
- container/process ID;
- first input received;
- first model decision;
- first speech token/audio chunk;
- playback start ACK;
- playback end ACK;
- user speech start/end;
- interruption detected;
- output epoch cancelled;
- stale audio dropped;
- task delegated;
- backend selected;
- permission request/decision;
- task completed;
- result_ready timestamp;
- gate wait reason(s);
- delivery started;
- delivery completed;
- memory write;
- memory retrieval;
- relevant memory IDs/source ranges.

Do not log raw secrets.

Avoid permanently storing raw microphone audio unless explicitly required for a test and retention is documented.

---

## PR sequence

Keep changes reviewable.

### PR 1 — Omni-SimpleMem becomes required GNSIS memory

- reuse/port shared sidecar;
- implement GNSIS provider;
- server-owned recall;
- cross-session semantic memory;
- episodic memory write/read;
- CodeMemory mirror/provenance;
- health/readiness;
- tests.

### PR 2 — Shared timeline + output epochs + delivery gating

- event model;
- playback ACK truth;
- result scheduler;
- stale-output protection;
- interruption/cancel path;
- tests + telemetry.

### PR 3 — Qwen Live Harness daemon/ACP integration

- direct daemon/adaptor reuse;
- Codex;
- Claude;
- permissions;
- cancellation;
- progress/result events;
- delivery gate integration.

### PR 4 — GNSIS desktop Host prototype

- adapt/fork Qwen Host where practical;
- GNSIS UI/branding boundary;
- capture/playback ACK;
- daemon lifecycle;
- permissions;
- real-device acceptance.

### PR 5 — Venus RealtimeProvider + Gander comparison

- Venus provider;
- same host/timeline/harness;
- benchmark scenarios;
- telemetry report;
- default-provider decision based on measured results.

If a PR naturally needs splitting further, do so. Do not collapse all of this into one risky rewrite.

---

## External implementations to use as source material

### Omni-SimpleMem

Upstream:
https://github.com/aiming-lab/SimpleMem

Existing hardened Clipit integration:
https://github.com/aubincorinaldiecooper-bit/CLIPIT/blob/main/tools/simplemem/sidecar.py

Relevant merged Clipit PRs:
- https://github.com/aubincorinaldiecooper-bit/CLIPIT/pull/107
- https://github.com/aubincorinaldiecooper-bit/CLIPIT/pull/109
- https://github.com/aubincorinaldiecooper-bit/CLIPIT/pull/114
- https://github.com/aubincorinaldiecooper-bit/CLIPIT/pull/123
- https://github.com/aubincorinaldiecooper-bit/CLIPIT/pull/126

### Qwen Live Harness

https://github.com/QwenLM/Qwen-Live-Harness

Focus on:

- `packages/qwen-live-harness/src/orchestrator`
- `packages/qwen-live-harness/src/realtime`
- `packages/qwen-live-harness/src/adaptor`
- `packages/qwen-live-harness/src/permissions`
- `packages/qwen-live-harness/src/subagents`
- `packages/qwen-live-harness/src/host`
- `packages/qwen-live-harness-host`

### Realtime-Venus

https://github.com/inclusionAI/Realtime-Venus

Use it as the first native-Omni foreground challenger.

### Current GNSIS baseline

- `runtime/configs/gnsis-voice.yaml`
- `runtime/gnsis_runtime/gnsis_runtime/memory_provider.py`
- `runtime/minicpm_ft/mcpmft/infer/detached_talker.py`
- `runtime/minicpm_ft/mcpmft/infer/realtime.py`
- `runtime/gnsis_runtime/gnsis_runtime/online_duplex.py`
- `src/gnsis/memory/base.py`
- `src/gnsis/service/repository.py`
- `docs/live_runtime.md`
- `docs/audits/2026-09-23-gnsis-full-experience-audit.md`

---

## Definition of done

This track is not done because the architecture is documented.

It is done when a real GNSIS desktop/live session can demonstrate this journey:

1. GNSIS sees and hears continuously.
2. The user asks for something that requires background work.
3. GNSIS acknowledges naturally without blocking the live interaction.
4. GNSIS delegates through the reused Harness/ACP layer.
5. The user keeps speaking/showing new information while the task runs.
6. GNSIS can remember relevant earlier audiovisual events through Omni-SimpleMem.
7. The worker completes.
8. The result enters `result_ready`, but is **not** spoken while the user or foreground response is active.
9. Actual device playback state opens the delivery gate.
10. GNSIS announces the result naturally.
11. The user interrupts mid-announcement.
12. The stale output epoch is cancelled promptly.
13. GNSIS continues from the new user intent without losing perception or task state.
14. The session ends.
15. A later session can recall an appropriate semantic or episodic memory from the prior session with provenance.

At that point GNSIS should feel like one realtime system even though perception, memory, task workers, and storage remain modular underneath.
