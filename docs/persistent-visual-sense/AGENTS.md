# AGENT.md — GNSIS Persistent Visual Sense Alignment

## Mission

Align the GNSIS desktop + runtime visual architecture with one hard product rule:

> **GNSIS has one persistent visual sense. Screen and camera are continuously captured through the live visual pipeline, and every still image used for perception must come from that same persistent stream.**

Do not create or preserve a parallel screenshot-based perception path.

This is an implementation brief, not another open-ended architecture study. The audit has already established that the core live path is sound but only partially aligned overall.

The job is to remove the remaining architectural drift and strengthen the persistent visual path without changing the product into a traditional screenshot-driven agent.

---

## Current verified baseline

The audit found:

1. **Capture is genuinely persistent.**
   - screen/camera source is acquired once;
   - the active `MediaStream` remains open;
   - visual frames are sampled from that stream;
   - capture is not reacquired per frame.

2. **The daemon already has one visual wire protocol.**
   - `screen.frame` is the only visual frame message;
   - `video_source: "screen" | "camera"` selects the source;
   - PR #107 aligned the desktop contract with that production daemon contract.

3. **A second screenshot path still exists.**
   - `ScreenshotAdapter`;
   - `ElectronScreenshots`;
   - `desktopCapturer.getSources(...).thumbnail.toPNG()`;
   - IPC `screenshot:capture`;
   - renderer/preload `captureScreenshot()`;
   - `capabilities.screenshots: true`.

   It is fully wired and callable but currently has no callers and no tool dependency. This is a second source of visual truth and must not remain part of the perception architecture.

4. **Recent temporal visual history is too thin.**
   - consumed frames are not retained as a reliable recent visual timeline;
   - the pending/latest frame surface is insufficient for dependable questions such as "what changed?" or "what was there two seconds ago?";
   - current conditional recent-media retention is not a sufficient explicit visual-history contract.

5. **Desktop capture is weaker than the existing production `video.js` path.**
   - desktop currently recreates `ImageCapture` while sampling;
   - desktop sampling is effectively hard-coded around 1 Hz;
   - desktop sends full-resolution JPEGs;
   - `ScreenClient` silently drops frames while the socket is unavailable;
   - the production `video.js` path already separates capture lifecycle from transport lifecycle, honors recommended frame rate, fits frames to 448 px, and has a reconnect budget.

Use these as the starting facts. Re-check the code before each implementation step, but do not re-litigate the product principle.

---

## Locked architectural decisions

### 1. One visual source of truth

The intended architecture is:

```text
screen / camera
      ↓
persistent acquisition
      ↓
continuous visual stream
      ↓
sample / encode frames
      ↓
shared GNSIS visual timeline
      ↓
current perception + recent temporal reasoning
```

Every still used for perception must be derived from this same path.

A still is:

> **a retained moment from the visual stream GNSIS already saw.**

A still is not:

> an independently reacquired screenshot.

### 2. Screen and camera are sources, not separate visual systems

Continue using:

```ts
{
  type: "screen.frame",
  video_source: "screen" | "camera"
}
```

Do not:

- restore `video.frame`;
- create a camera-specific websocket;
- create a second model-side visual path;
- introduce separate visual memory for screen vs camera unless source metadata alone is insufficient for a proven reason.

### 3. JPEG/frame transport is not the same as screenshot architecture

It is acceptable for the realtime model to consume sampled JPEG frames.

The key distinction is:

```text
persistent stream → sampled frame = acceptable
```

versus:

```text
independent OS screenshot acquisition → separate visual input = not acceptable
```

Do not "fix" JPEG frame transport merely because the payload is image-shaped.

### 4. Capture lifecycle and transport lifecycle are separate

The user granting screen/camera capture establishes a persistent capture session.

A websocket failure must not automatically tear down and reacquire the screen/camera source.

Desired behavior:

```text
capture remains alive
        │
transport disconnects
        │
transport reconnects
        │
current perception resumes
```

Do not use capture reacquisition as a reconnect mechanism.

### 5. Do not backlog stale perception by default

If transport is unavailable, do not automatically queue an unbounded backlog of JPEG frames for later replay.

For realtime perception, stale frames can be worse than dropped frames.

Prefer:

- bounded recent visual retention;
- reconnect transport;
- resume from current visual state;
- use retained recent history only where temporal reasoning explicitly needs it.

### 6. Recent visual history must be bounded

GNSIS needs enough recent visual history to reason about what just happened, but it must not create an unbounded screen recording.

Use a bounded timestamp-indexed recent frame/history structure.

Prefer the existing `codex_screen_history_seconds` horizon unless code inspection identifies a stronger existing source of truth.

Do not create indefinite raw-media retention.

### 7. Still inspection APIs are demand-driven

Do not invent a large still-image API before there is a consumer.

The underlying architecture should make future operations possible, such as:

- latest frame;
- recent frame;
- frame at timestamp;
- pinned frame;
- higher-detail inspection.

But implement a public contract only when a real consumer requires one.

The data must already come from the persistent visual stream.

---

## Implementation sequence

Implement this as small focused PRs.

Do not collapse all work into one large visual rewrite.

---

# PR A — Remove the independent screenshot architecture

## Goal

Delete the second screenshot-based visual path so GNSIS has one visual source of truth.

## Remove

Trace and remove the unused independent screenshot surface, including where present:

- `ScreenshotAdapter`;
- `ElectronScreenshots`;
- `screenshot:capture`;
- `captureScreenshot()`;
- `capabilities.screenshots`;
- related imports;
- related preload types;
- related renderer declarations;
- stale docs/tests that describe native screenshot capture as a GNSIS perception capability.

Search the full repository before finishing.

Relevant search concepts include:

```text
ScreenshotAdapter
ElectronScreenshots
captureScreenshot
screenshot:capture
screenshots
desktopCapturer
thumbnail.toPNG
```

Do not blindly delete unrelated test/debug/export tooling with the word "screenshot". Classify each hit.

## Preserve

Do not disturb:

- persistent screen capture;
- persistent camera capture;
- `screen.frame`;
- `video_source`;
- microphone path;
- playback;
- HostSession;
- internet/tool execution;
- packaging;
- permissions required for live screen/camera capture.

## Acceptance criteria

PR A is complete when:

1. no callable independent screenshot-perception path remains;
2. `HostCapabilities` no longer advertises screenshot perception;
3. desktop preload/renderer/main no longer expose screenshot capture for perception;
4. live screen and camera capture still work through the persistent media path;
5. no daemon protocol change is required;
6. desktop typecheck/tests/build are green;
7. relevant runtime tests remain green;
8. repository search is included in the PR report showing what screenshot-related references remain and why.

Stop after opening PR A. Do not begin PR B on the same branch.

---

# PR B — Align desktop live capture with the proven production path

## Goal

Make the packaged desktop Host use the same good realtime capture principles already proven in the production `runtime/minicpm_ft/.../video.js` path.

Do not rewrite the architecture from scratch.

## Mandatory baseline measurement

Before changing capture behavior, measure the current desktop path using the existing benchmark/telemetry surfaces where possible.

Record at minimum:

- configured sampling rate;
- actual frames captured/sent;
- frame dimensions;
- encoded bytes/frame;
- capture → IPC timing if measurable;
- IPC → daemon receive timing if measurable;
- CPU impact if available;
- source switch behavior;
- disconnect/reconnect behavior.

If `bench.ts` is the existing appropriate harness, extend/reuse it rather than adding a parallel benchmark system.

## Persistent source

Keep one active screen/camera source open.

Do not reacquire the capture source per frame.

## Frame extraction

Audit the current pattern around:

```ts
new ImageCapture(track)
grabFrame()
canvas.drawImage(...)
canvas.toBlob(...)
setInterval(...)
```

Avoid recreating `ImageCapture` every sample if unnecessary.

Compare against the proven browser/production path before selecting the implementation.

If a video-element/`drawImage`, `requestVideoFrameCallback`, WebCodecs, or another existing mechanism materially reduces overhead or improves correctness, justify it with measurement.

Do not change APIs merely because they are newer.

## Frame-rate contract

The desktop path must honor the runtime's `recommended_frame_rate` where that capability is already supplied.

Do not leave 1 Hz as a hidden hard-coded desktop behavior unless the runtime explicitly recommends 1 Hz.

Maintain reasonable guardrails for invalid/zero values.

## Resolution / fit

Align with the production path's 448 px fit unless benchmarking or model-contract evidence demonstrates a different required size.

Do not send full-resolution desktop images by default simply because the source provides them.

Record:

- source dimensions;
- transmitted dimensions;
- approximate encoded bytes;
- any quality setting.

## Transport reconnect

Give the desktop `ScreenClient`/visual transport a bounded reconnect strategy equivalent in principle to the working `video.js` path.

The important invariant is:

> transport can recover underneath a still-active capture session.

On ordinary websocket interruption:

- keep capture active;
- reconnect transport;
- do not reacquire the OS capture source solely because the websocket dropped;
- resume current perception after reconnection.

Do not create an unlimited frame outbox.

If a tiny bounded handoff buffer is required to avoid races, prove why and keep it bounded.

## Source lifecycle

Explicit user/source/session actions may end capture.

Examples:

- user stops sharing;
- capture track is ended by the OS/browser;
- permission revoked;
- source intentionally switched;
- app/session teardown.

Transport failure alone should not masquerade as a capture-ending event.

## Acceptance criteria

PR B is complete when:

1. the screen/camera source is acquired once per active source lifecycle;
2. the frame sampler no longer recreates heavyweight capture state per tick without reason;
3. desktop honors `recommended_frame_rate`;
4. transmitted frames follow the agreed fit policy;
5. transport disconnect does not tear down/reacquire the capture source;
6. transport reconnect is bounded and observable;
7. no stale unlimited frame backlog is introduced;
8. screen and camera still share `screen.frame`;
9. before/after benchmark data is included;
10. desktop tests/typecheck/build and relevant runtime tests are green.

Stop after opening PR B. Do not begin PR C on the same branch.

---

# PR C — Add bounded recent visual history

## Goal

Give GNSIS dependable recent temporal visual continuity without introducing screenshots, a second visual memory, or full raw screen recording.

The required product behavior is that GNSIS can reason about recent visual change from frames it already received.

Representative questions:

- "What changed?"
- "What was on screen two seconds ago?"
- "Where did that object move?"
- "What happened immediately before this?"

These should not require a new screen capture.

## Placement

Start beside the existing runtime visual buffering path, including `LatestScreenFrameBuffer`.

Inspect actual ownership before implementation.

Prefer one bounded visual-history structure rather than parallel recent-frame stores.

## Retention

Use timestamp-indexed bounded retention.

Prefer the existing `codex_screen_history_seconds` horizon if it is still the correct configured recent-history window.

Retention must be bounded by time and/or count/memory.

Do not create:

- unbounded frame arrays;
- implicit long-term screen recording;
- duplicate history stores for screen and camera;
- a persistent filesystem video archive for this feature.

## Metadata

Retained visual entries should preserve enough information to remain useful in the shared causal timeline.

At minimum evaluate:

- frame ID;
- source capture timestamp;
- server receive timestamp if already tracked;
- `video_source`;
- dimensions;
- encoding/reference;
- call/session identity where required by existing timeline boundaries.

Do not invent redundant timestamps if the shared timeline already owns them.

## Current + recent behavior

The latest-frame surface should continue to support current perception efficiently.

Recent history should support bounded temporal lookup/reuse.

Do not force every model call to ingest the entire retained history.

Use history only where relevant to the existing front-brain/back-brain logic.

## Memory integration

Feed retained recent visual context into the existing architecture where appropriate:

- front-brain recent-media reuse;
- back-brain / `remember_media`;
- shared timeline / episodic memory boundaries.

Do not create a separate "visual memory system" adjacent to Omni-SimpleMem or the existing session timeline.

Recent raw/encoded frames are short-horizon perception state.

Durable semantic/episodic memory remains a separate higher-level concern.

## Still-image rule

Any future still-image inspection must select from this persistent visual path/history.

Do not reintroduce `ScreenshotAdapter`.

If a high-resolution inspection capability is later required, it must derive from the already active capture source and remain part of the same visual lifecycle.

## Acceptance criteria

PR C is complete when:

1. recently consumed visual frames have a bounded timestamped history;
2. history is pruned deterministically;
3. current perception remains low-latency;
4. recent temporal reasoning can access frames GNSIS already saw;
5. no independent screenshot acquisition is used;
6. screen/camera source metadata remains intact;
7. session/call boundaries prevent stale visual history leaking into a new context;
8. memory/resource growth is bounded and tested;
9. relevant runtime tests prove ordering/pruning/source/session behavior;
10. no new wire protocol is required.

Stop after opening PR C.

---

## Tests that must exist by the end of this workstream

Across the PR sequence, establish regression coverage for:

- screen and camera both use `screen.frame`;
- source switch changes `video_source`, not event type;
- capture source remains persistent across multiple sampled frames;
- no independent screenshot perception route remains;
- frame timestamps/order are preserved;
- transport reconnect does not require capture reacquisition;
- stale transport frames are not replayed as current perception;
- recent history is bounded;
- history pruning works;
- session/call boundaries prevent stale-frame reuse;
- screen and camera history can be distinguished by source metadata;
- still/recent-frame reasoning uses previously captured stream frames.

Do not create brittle tests that merely grep exact comments.

Test behavior and contracts.

---

## Telemetry requirements

Make it possible to distinguish capture health from transport health.

Useful events/metrics include:

- capture started/stopped;
- capture source;
- source dimensions;
- configured frame rate;
- frame sampled;
- frame encoded;
- encoded bytes;
- frame sent;
- frame dropped because transport unavailable;
- transport disconnected;
- transport reconnect attempt;
- transport reconnected;
- recent-history insert/prune/count;
- source switch;
- capture ended by user/OS/permission.

Do not routinely log raw screen/camera pixels.

Routine telemetry should be metadata/timing only.

---

## Privacy boundary

Persistent visual sense does **not** mean persistent raw recording.

The default architecture should:

- perceive continuously while the user has enabled capture;
- retain only a bounded recent working set needed for temporal reasoning;
- avoid durable raw-media storage by default;
- preserve source-time metadata for higher-level episodic memory;
- make any explicit debugging raw-media capture opt-in and time-bounded.

Do not silently turn recent-frame retention into screen recording.

---

## Things not to change in this workstream

Do not mix this visual alignment with:

- voice/TTS replacement;
- Gander/Venus model comparison;
- model quantization;
- local-runtime conversion;
- DMG signing/notarization;
- Tauri migration;
- unrelated tool integrations;
- general UI redesign;
- memory-system replacement.

Do not rewrite the daemon protocol.

Do not modify `screen.frame` merely because its name sounds screenshot-like. It is the established neutral visual-frame event.

---

## Final architecture target

At completion, the system should be accurately described as:

```text
GNSIS persistent visual sense

screen / camera
      │
      ▼
one active capture lifecycle
      │
      ▼
frame sampling / fit / encode
      │
      ▼
screen.frame
      │
      ▼
runtime current-frame buffer
      │
      ├── current realtime perception
      │
      └── bounded timestamped recent history
                    │
                    ├── temporal reasoning
                    ├── recent-media reuse
                    └── still/deeper inspection when a consumer requires it
```

There is no parallel screenshot-perception subsystem.

---

## Definition of done

This workstream is done when all of the following are true:

- GNSIS has one visual acquisition path for perception;
- screen and camera are two sources of that one path;
- every still used for perception originates from that persistent path;
- no independent screenshot perception API exists;
- capture survives ordinary transport interruption;
- runtime can reason over a bounded recent visual window;
- recent history is bounded and session-safe;
- current perception remains efficient;
- no unnecessary new protocol or memory subsystem was introduced;
- tests and telemetry protect the architecture from drifting back toward screenshot-driven perception.

When reporting completion, include:

1. PR links for A/B/C;
2. before/after visual architecture;
3. benchmark results from PR B;
4. exact retention policy from PR C;
5. remaining known limitations;
6. any deferred still-inspection API and the concrete consumer that would justify it.

Do not claim full alignment until all acceptance criteria above are proven.
