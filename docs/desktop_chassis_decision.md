# Desktop chassis decision and Tauri exit criteria

**Status:** Accepted for current implementation  
**Current chassis:** Electron, using the Qwen Live Harness Host as the primary reuse/reference path  
**Fallback chassis:** Tauri, with ClickyX as a useful implementation reference  
**Scope:** GNSIS desktop shell only. This decision must not leak into the realtime intelligence, memory, task, or model architecture.

## Decision

GNSIS will stay on Electron for the current desktop implementation.

We are not choosing Electron because it must remain the permanent chassis. We are choosing it because the Qwen Live Harness Host already solves a large amount of desktop plumbing that GNSIS needs now:

- microphone capture;
- camera and screen capture;
- macOS permission handling;
- streaming audio playback;
- playback-start and playback-complete acknowledgements;
- output/call epochs;
- daemon reconnect/lifecycle;
- device switching;
- native screenshots;
- renderer isolation;
- packaging/signing patterns.

The objective is to ship the realtime desktop experience without rebuilding solved host infrastructure.

At the same time, the desktop boundary must remain replaceable. If Electron becomes a material source of latency, instability, memory/CPU overhead, capture reliability problems, or cross-platform friction, GNSIS should be able to replace the shell with Tauri without rewriting the intelligence system.

## What "modular desktop boundary" means

The desktop app is a **host**, not GNSIS itself.

Electron may own:

- application windows and tray/menu behavior;
- OS permissions;
- microphone/camera/screen devices;
- capture encoders/resamplers;
- speaker playback;
- native screenshots;
- local keyboard/global shortcuts;
- desktop notifications;
- update/install lifecycle;
- the visible UI.

Electron must **not** own:

- realtime-model semantics;
- memory;
- task orchestration;
- ACP/backend agents;
- delivery-gating policy;
- conversation state;
- durable task state;
- model-provider selection;
- GNSIS reasoning logic.

Those remain behind the GNSIS daemon/runtime boundary.

The desired shape is:

```
            GNSIS DESKTOP HOST
        Electron now / Tauri later
                   |
        versioned local protocol
                   |
                   v
              GNSIS DAEMON
                   |
     +-------------+--------------+
     |             |              |
 realtime       memory         task/harness
 provider     Omni-SimpleMem     ACP/etc.
```

A future Tauri migration should therefore replace:

```
Electron Host
```

with:

```
Tauri Host
```

while keeping the daemon, model providers, memory system, task layer, shared timeline, and delivery gating intact.

## Stable host contract

Do not expose Electron-specific objects or APIs to the daemon.

The host/daemon protocol should use neutral GNSIS messages such as:

- `audio.frame`
- `screen.frame` (one visual-frame type; `video_source` selects screen vs camera)
- `device.changed`
- `permission.changed`
- `playback.started`
- `playback.completed`
- `playback.cancelled`
- `call.started`
- `call.ended`
- `host.ready`
- `host.disconnected`

Every media/control message should carry the IDs/timestamps required by the shared GNSIS timeline.

The daemon should never need to know whether a frame came from Electron, Tauri, a browser, or another host.

## Adapter rule

Put OS/chassis-specific behavior behind small interfaces.

Examples:

```ts
interface AudioCaptureAdapter {
  start(): Promise<void>
  stop(): Promise<void>
  onFrame(cb: (frame: AudioFrame) => void): void
}

interface VideoCaptureAdapter {
  start(source: CaptureSource): Promise<void>
  stop(): Promise<void>
  onFrame(cb: (frame: VideoFrame) => void): void
}

interface PlaybackAdapter {
  enqueue(chunk: AudioChunk): Promise<void>
  cancel(outputEpoch: string): Promise<void>
  onStarted(cb: (event: PlaybackStarted) => void): void
  onCompleted(cb: (event: PlaybackCompleted) => void): void
}

interface DesktopPermissionAdapter {
  status(): Promise<PermissionState>
  request(kind: PermissionKind): Promise<PermissionState>
}
```

The rest of GNSIS should depend on these contracts, not Electron modules.

## Performance acceptance targets

These are **desktop-chassis budgets**, not end-to-end model latency targets. Network/model inference time must not be blamed on the Host.

Use real-device telemetry to measure them.

### Launch and recovery

- Warm desktop launch to usable UI: target <= **3 seconds**.
- Host <-> daemon reconnect after an ordinary daemon restart/disconnect: target <= **2 seconds** once the daemon endpoint is available.
- Reconnect must not replay stale audio or duplicate a completed background result.

### Audio path

- Local microphone capture -> daemon receive overhead: target p95 <= **75 ms**.
- First audio buffer received by Host -> actual playback start acknowledgement: target p95 <= **100 ms**.
- No persistent underruns, gaps, duplicated chunks, or stale output after cancellation in a **30-minute** live session.
- A stale output epoch must never continue after a newer epoch owns playback.

### Visual path

For the initial realtime target of a 1 FPS live screen/camera feed:

- sustain the configured feed for **30 minutes**;
- target < **1%** host-side frame loss attributable to capture/transport;
- capture must not freeze or require restarting the app after source switches;
- source timestamps must remain monotonic and usable by the GNSIS timeline.

These numbers should scale if the required feed rate changes later.

### Resource use

Measure the complete desktop Host process group, not a single renderer.

Initial review thresholds:

- idle Host CPU: target average <= **5% of one CPU core** on a representative supported Mac;
- active 1 FPS capture + microphone + playback-ready state: target average <= **25% of one CPU core**, excluding daemon/model processes;
- steady-state Host RSS: target <= **500 MB**;
- after a 30-minute live session, retained Host memory should not grow > **20%** from the stabilized post-start baseline without returning after the session ends.

These are practical starting budgets, not permanent product promises. If empirical baselines show a target is inappropriate, change the target in a dedicated PR with measurements and rationale rather than silently weakening it.

## Reliability acceptance

Electron is acceptable only if the Host can repeatedly pass:

1. microphone permission grant/revoke/re-grant;
2. screen-recording permission grant/revoke/re-grant;
3. camera permission grant/revoke/re-grant;
4. screen source switch;
5. camera source switch;
6. audio device switch;
7. speaker device switch where supported;
8. sleep/wake;
9. daemon restart;
10. network interruption affecting the remote realtime provider;
11. output cancellation during playback;
12. repeated call start/end cycles;
13. a 30-minute full-duplex session.

A single test failure is a bug, not automatically a reason to migrate chassis. We revisit the chassis when failures are structural or persistent after a bounded optimization pass.

## Tauri review triggers

Open a formal Tauri migration evaluation if **any hard blocker** occurs:

- Electron cannot provide required capture/permission behavior reliably on a target OS;
- Electron prevents a required Windows/Linux release;
- playback/capture correctness cannot meet the GNSIS full-duplex contract;
- Electron-specific architecture prevents output-epoch cancellation or accurate playback acknowledgements;
- a security/sandboxing requirement cannot reasonably be met.

Also open a Tauri evaluation if, after one focused optimization pass, Electron persistently misses **two or more** of these experience/resource targets:

- launch/reconnect targets;
- local audio-path latency targets;
- visual capture reliability target;
- idle/active CPU budgets;
- steady-state/leak memory budgets;
- 30-minute stability acceptance.

Do not trigger a rewrite because one benchmark is marginally worse.

## Migration proof required

If a Tauri migration is proposed, require a small Tauri/ClickyX-inspired prototype against the **same GNSIS daemon contract**.

The prototype must measure the same telemetry as Electron.

A chassis migration should demonstrate one of:

- fixes a hard blocker Electron cannot fix;
- materially improves a user-facing latency/reliability failure;
- materially reduces resource use that is causing a product problem;
- unlocks a required operating system with less overall risk than extending Electron.

Prefer measured improvement over architectural preference.

A useful resource improvement should be large enough to matter in practice; as a starting comparison, look for roughly **30%+** improvement in the resource metric that triggered the review unless the change instead resolves a hard correctness/platform blocker.

## What is not a migration reason

Do not migrate merely because:

- Tauri binaries can be smaller;
- Rust is considered more native;
- another application uses Tauri;
- Electron has a reputation for being heavy;
- ClickyX demonstrates an attractive chassis;
- a synthetic microbenchmark is faster while the real GNSIS session is already within budget.

The decision is based on the GNSIS experience.

## Cross-platform rule

Do not let macOS-only Host APIs leak into the GNSIS daemon contract.

When Windows/Linux work starts, implement platform-specific capture/permission adapters behind the same Host protocol.

If Qwen Host code being reused is macOS-specific, keep that specificity isolated inside the Electron Host.

This is what preserves a later choice between:

- extending Electron across platforms; or
- swapping the Host implementation to Tauri.

## Telemetry required before desktop acceptance

Record at minimum:

- app launch timestamp;
- Host ready;
- daemon connected;
- permission request/result;
- capture start/stop;
- audio frame captured;
- audio frame received by daemon;
- screen/camera frame captured;
- frame received by daemon;
- audio chunk received by Host;
- playback scheduled;
- playback actually started;
- playback actually completed;
- playback cancelled;
- current output epoch;
- stale chunk rejected;
- daemon disconnected/reconnected;
- Host RSS/CPU samples;
- capture frame counts/drops.

Do not log raw secrets.

Any recording of raw user audio/video for diagnostics must be explicit, time-bounded, and removable; routine telemetry should use metadata/timestamps rather than retaining content.

## Current implementation guidance

For the current implementation:

1. stay on Electron;
2. evaluate/adapt Qwen Live Harness Host rather than rebuilding desktop plumbing;
3. port useful ClickyX chassis ideas only where they improve GNSIS;
4. preserve the neutral host/daemon protocol;
5. add the performance telemetry above as the desktop work lands;
6. run the acceptance suite before calling Electron "good enough";
7. if the review triggers fire, open a Tauri spike against this same contract.

## Revisit record

If a future PR proposes Tauri, it should link back to this document and include:

- failing Electron measurements;
- optimization attempted;
- Tauri prototype measurements;
- compatibility delta;
- migration cost/risk;
- recommendation based on the observed GNSIS experience.

This keeps the future decision evidence-based and prevents the desktop chassis from becoming coupled to the intelligence architecture.
