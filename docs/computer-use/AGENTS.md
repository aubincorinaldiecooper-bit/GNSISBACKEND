# AGENT.md — Computer Use Activation

## Mission

Finish the last-mile computer-use implementation for GNSIS by attaching concrete local action adapters to the execution architecture that already exists.

This is **not** a new architecture project.

The goal is to move from:

```text
SEE / HEAR
   ↓
UNDERSTAND
   ↓
DECIDE
   ↓
[execution seam exists, but few real local actions are exposed]
```

to:

```text
SEE / HEAR
   ↓
UNDERSTAND
   ↓
DECIDE
   ↓
ACT
   ↓
SEE THE RESULT
   ↓
VERIFY
   ↓
ADAPT / RECOVER IF NEEDED
   ↓
COMPLETE
```

The target product behavior is ordinary computer use: GNSIS should be able to operate the user's real machine and existing browser session without the user enumerating every click.

Do not redesign perception, memory, speech, delivery gating, the desktop chassis, or the realtime provider to accomplish this.

---

## Current state — verify before implementation

Before changing code, inspect current `main` and confirm the existing implementation.

The expected baseline already includes:

- GNSIS Desktop Host;
- versioned Host <-> daemon event seam;
- `host.event`;
- `ToolRegistry` on the desktop side;
- `HarnessDaemonClient`;
- `HarnessBridge`;
- shared `SessionTimeline`;
- delivery gating / output epochs / playback ACK truth;
- permission requests represented as explicit state, never implicit grants;
- continuous screen perception;
- bounded recent visual history;
- source/session reset that clears stale visual history;
- model-agnostic realtime provider boundary;
- persistent visual verification capability after an action.

Also confirm the important current limitations:

> The packaged Desktop `ToolRegistry` currently exposes only `internet_search`, and it is reachable only through renderer IPC — the runtime's external tool calls have no Host execution/response path.

Those are the known last-mile gaps. Do not mistake them for an absence of execution architecture — but do not treat adapter registration as the sole remaining work either (see the tool-routing requirement below).

### Verified Desktop baseline findings

The following seven findings were re-checked against current main before this implementation program.

#### Already resolved — do not reopen unless regression evidence appears

1. **Packaged preload ESM mismatch — resolved in #106.**
   - packaged preload is emitted as `preload.mjs`;
   - `BrowserWindow` points at the ESM preload;
   - do not spend this program re-solving the old `.js`/CommonJS preload failure.

2. **Screen socket opening before token — resolved in #111.**
   - `screen.connect()` records intent only;
   - actual open is gated on the daemon-issued screen token;
   - authenticated open occurs after `ready` / `media.mode.done` via `applyChannel`;
   - do not reintroduce token-less eager screen connection.

#### Real Host correctness bugs — must be fixed before computer-use latency/reliability baselines are trusted

1. **Unhandled WebSocket `error` event**
   - `wsClient.ts` re-emits `error` from duplex and screen sockets;
   - `HostSession` currently attaches no error listener;
   - DNS failure / refused upgrade can therefore become an uncaught Electron-main exception.
   - Required fix: subscribe to socket errors, normalize/surface them as Host transport state/telemetry, keep the process alive, and let bounded reconnect own recovery.

2. **Overlapping playback chunks**
   - renderer playback currently calls `src.start()` immediately for each Talker chunk;
   - there is no accumulated `nextStartTime`;
   - streamed chunks can overlap rather than form one continuous output queue.
   - Required fix: schedule each chunk at `max(audioContext.currentTime, nextStartTime)`, then advance `nextStartTime` by the scheduled buffer duration; reset the schedule correctly on cancellation/output-epoch change.

3. **Daemon `playback.cancel` is not authoritative in the Host**
   - the current control path logs the cancellation but does not stop buffered/current audio;
   - stale speech can continue after daemon cancellation, breaking barge-in/output-epoch truth.
   - Required fix: route cancellation into the playback owner, stop current source(s), invalidate queued stale chunks, and emit authoritative `playback.cancelled` with the matching playback/output epoch.

4. **Audio-worklet resampler phase drift**
   - fractional source position currently restarts at zero for each worklet `process()`;
   - at 44.1 kHz input with 128-frame callbacks this overproduces samples relative to the intended 16 kHz stream.
   - Required fix: carry fractional resampling phase/source position across calls and verify long-run output ratio, timestamp/sample-count continuity, and absence of cumulative drift.

5. **Mic-off currently terminates the session**
   - `stopMic` sends terminal `{"type":"stop"}`;
   - later `startMic` calls `startCall()` against a dead socket/session and can leave frames queued while UI reports mic on.
   - Required fix: define the mic button as local mute/unmute (stop/reacquire local tracks without ending the session) or explicitly rebuild/reconnect the session before accepting new frames. The UI must never report "mic on" until the transport/session is actually usable.

These five bugs are not computer-use architecture changes. They are prerequisites for trustworthy end-to-end measurement because transport crashes, overlapping/stale playback, audio clock drift, or dead-session mic state would otherwise contaminate acceptance results.

### Mandatory gap inventory

Before implementation, produce a concise matrix from the actual code:

| Capability | Existing seam | Concrete adapter present? | Packaged/reachable? | Missing work |
|---|---|---:|---:|---|
| Internet search | ToolRegistry | yes | yes | |
| Filesystem | | | | |
| App/process launch | | | | |
| Shell/process action | | | | |
| Existing browser session | | | | |
| Generic desktop UI | | | | |
| Open-ended fallback | Harness/ACP | | | |
| Action telemetry | timeline/Host | | | |
| Post-action visual verification | vision/history | | | |

Do not create a parallel execution stack if an existing seam already fits.

---

## Critical preconditions that must be closed first

These are implementation gaps discovered in the current code. Treat them as prerequisites, not optional polish.

### 1. Close the model -> Host -> model local-tool loop

The runtime already supports model-generated external/business tool calls and a later `tool.response` injection back into the live model.

The Desktop currently has a `ToolRegistry`, but the packaged path only invokes it directly from renderer IPC. There is not yet a complete broker that:

```text
front-brain emits local business tool.call
        ↓
runtime sends call to the owning Desktop Host
        ↓
Host validates + executes ToolRegistry adapter
        ↓
Host returns correlated tool.response
        ↓
runtime feeds response back into the same live model session
```

Do not build local adapters before proving this path end-to-end. Otherwise tools can exist on the Mac without being callable by GNSIS.

Requirements:

- preserve one pending external-tool call contract unless the runtime is deliberately extended;
- correlate every call/result to the correct session and call;
- reject unknown/unadvertised tools;
- make disconnect/cancellation behavior deterministic;
- never execute the same call twice after reconnect/retry;
- return bounded structured errors to the model;
- keep renderer buttons/debug UI out of the authoritative execution path.

### 2. Keep Host capabilities and model-visible tool schemas in sync

The runtime can expose configured business-tool schemas to the realtime model, while the Desktop owns the concrete local implementations.

Those two catalogs must not drift.

Before execution begins, establish a small capability/manifest contract so:

- the model is not shown a local tool the connected Host cannot execute;
- a Host adapter is not silently installed but invisible to the model;
- schema/version mismatches fail closed;
- session reconnect preserves the same negotiated capability set or explicitly renegotiates it.

Respect the realtime model's tool-schema count/token budget. Do not expose dozens of tiny OS primitives if a smaller, well-typed capability surface can express the same actions.

Prefer a compact tool surface with explicit action arguments over schema explosion, while keeping permissions specific enough to audit.

### 3. Treat local execution as a trust boundary

Once a runtime can ask the Host to move files, type into applications, or control a logged-in browser, the runtime connection is no longer only media transport.

Before enabling side-effecting actions:

- verify how the packaged Host authenticates/trusts the runtime endpoint in local and remote configurations;
- do not accept executable local-tool requests from an arbitrary runtime URL merely because a WebSocket connected;
- bind action requests to the active authenticated/trusted session;
- preserve explicit user permission for risky actions;
- fail closed on trust/capability mismatch.

Reuse existing GNSIS edge/session authentication where it actually applies; do not invent a second auth system unless the current boundary cannot protect Host execution.

### 4. Add the macOS Accessibility/native-control permission path

Microphone, camera, and Screen Recording permission are not sufficient for generic desktop control.

On macOS, UI automation must account for Accessibility trust and, depending on the chosen mechanism, Automation/Apple Events consent.

Treat these as different permission models:

- **Accessibility** is process/app trust for controlling UI through accessibility/native automation surfaces.
- **Automation / Apple Events** consent is not one universal global toggle. Consent is evaluated for the controlling application and the concrete target application being automated, with behavior depending on the mechanism used.

The generic desktop implementation must include:

- Accessibility status/preflight;
- a clear user path to request/grant Accessibility trust;
- correct behavior when Accessibility is denied/revoked;
- relaunch/retry behavior where macOS requires it;
- discovery of whether the selected control mechanism actually uses Apple Events;
- for Apple-Events-driven actions, target-aware Automation consent handling rather than a fake global `automation=true/false`;
- `NSAppleEventsUsageDescription` packaging metadata when Apple Events are used;
- a small native helper/bridge if Electron/Node cannot provide the required OS API directly;
- telemetry that distinguishes Accessibility denial, target-specific Automation denial, unsupported automation mechanism, and ordinary navigation failure.

Do not report generic desktop control as available merely because screen capture works.

Do not invent a universal Automation status API if macOS does not expose one for the selected mechanism. Measure the consent/result against the concrete target application/action being attempted.

### 5. Verify the real-browser attachment mechanism before building browser semantics

The product requirement is the user's existing authenticated browser session.

Do not assume ordinary Chrome remote-debugging flags can always attach to the user's default active profile. Verify current Chrome/browser security constraints and the current upstream `open-browser-use` path first.

Prefer a local extension/native-host or similarly supported attachment mechanism that can control the already-running browser without copying credentials.

Acceptance must prove:

- connect to the intended browser/profile;
- identify current tabs;
- preserve existing login state;
- detach without killing the user's browser;
- recover from browser restart/disconnect;
- no cookie/password material enters model context.

If installation currently requires a browser extension or helper, treat that as a real packaging/onboarding dependency and document it rather than hiding it.

### 6. Do not assume Harness can launch the open-ended fallback

The current `HarnessDaemonClient`/ `HarnessBridge` surface monitors subagent snapshots and controls stop/permission/result flow. The normal `task_start` path is backed by the configured worker provider registry.

Therefore Phase 0 must prove how an OpenHands/open-ended desktop navigator is actually started.

If no provider/launch adapter exists:

- add the smallest provider/launch integration behind the existing task system;
- do not overload the monitoring bridge into a second task system;
- make task start/progress/permission/terminal state flow through the existing Gateway/timeline/delivery machinery.

---

## Locked architecture

### 1. GNSIS remains the owner of the user relationship

GNSIS owns:

- user intent;
- realtime perception;
- memory;
- decision-making;
- session truth;
- delivery policy;
- speech;
- the shared timeline;
- task/result presentation.

Execution backends do not become a second assistant.

### 2. Use the existing execution seams

Use the smallest existing boundary that fits each action.

Expected split:

```text
known / deterministic local action
        ↓
direct local primitive / ToolRegistry-compatible adapter

open-ended delegated work
        ↓
Harness / ACP backend

unfamiliar visual UI
        ↓
generic desktop navigator/fallback
        ↓
same GNSIS result + verification path
```

Do not make every file move or app launch a background subagent task.

Do not route every action through OpenHands.

Do not create another delivery/result channel.

### 2a. Bidirectional tool routing is required work

The runtime emits external tool calls and then waits for a client `tool.response`. Today the desktop `ToolRegistry` is invoked only through renderer IPC (a hard-coded UI button), while incoming model-originated tool-call controls are merely logged — no Host path executes the call and returns a correlated response, so a model-issued call leaves the external tool pending forever.

The Host must implement both directions explicitly:

```text
runtime external tool call (control on the duplex channel)
        ↓
HostSession routes it to ToolRegistry by name
        ↓
adapter executes (with provenance/permission checks)
        ↓
Host sends correlated tool.response back to the runtime
```

This routing is a prerequisite for every adapter in this program: a registered tool the model cannot reach is not callable. Include it in the earliest adapter PR — do not describe adapters as the sole last-mile gap.

### 3. GNSIS vision remains the verification layer

Do not add a second competing visual brain by default.

After an action:

```text
action issued
    ↓
action result/event
    ↓
GNSIS observes current screen
    ↓
expected state visible?
   /      \
 yes       no
  ↓         ↓
complete   adapt / retry / escalate
```

The executor may report success, but executor success is not automatically user-goal success.

Use the existing screen stream and recent visual history to verify observable outcomes.

### 4. Deterministic primitives first

Prefer direct local primitives when the intent maps cleanly to an operating-system action.

Examples:

- list/find/open/move/rename files;
- create folders;
- launch/focus/quit applications;
- open a path or URL;
- safe process inspection;
- bounded shell/process actions where appropriate.

Do not click through a GUI when a direct local primitive can complete the same task more reliably.

### 5. Existing browser session is the priority browser target

The desired behavior is control of the user's existing browser/profile/session where technically viable.

Preserve:

- current tabs;
- existing logins;
- cookies/session state;
- the user's active browsing context.

Investigate/reuse an `open-browser-use`-style bridge or another proven local browser-control mechanism.

Do not make disposable isolated Chromium the default merely because it is easier.

Do not copy raw cookies, passwords, browser credentials, or auth databases into model context.

The browser-control bridge should act locally and return normalized action/result metadata.

### 6. Generic desktop control is a fallback, not the first tool

When no deterministic primitive or browser-specific action fits, use a generic desktop interaction path for:

- click;
- type;
- scroll;
- focus;
- select;
- keyboard shortcut;
- basic UI navigation.

Prefer stable accessibility/native automation surfaces where available.

Do not hard-code brittle pixel coordinates as the primary contract.

Any coordinate-based fallback must be treated as fallible and followed by visual verification.

### 7. Open-ended navigation is a fallback

OpenHands or another open-ended navigator may help when:

- the environment is unfamiliar;
- the path to completion is not known;
- several adaptive steps are required.

It is an executor/navigation fallback only.

GNSIS still owns:

- perception;
- task intent;
- permissions;
- memory;
- user-facing state;
- delivery;
- final verification.

---

## Security and permission rules

Computer use must remain least-privilege and attributable.

### Never silently grant permissions

Existing Harness permission behavior is the model:

```text
backend requests permission
        ↓
timeline event / pending request
        ↓
explicit allow or deny
        ↓
decision returned to backend
```

Do not turn a permission request into an automatic grant.

### Bind local actions to authenticated user intent

When continuous screen/browser perception is active, untrusted text on a webpage or in a document can induce a model-generated tool call — indirect prompt injection that would operate the user's real machine. Approval-only-when-a-backend-asks is therefore insufficient.

Every external tool call must carry attributable provenance, using the same trusted `turn.final` binding the task tools already rely on.

At minimum record:

- the trusted user turn ID, when one exists;
- whether the relevant intent came from direct user speech/text, observed screen/page/document content, delegated-task output, or a mixture;
- the requested capability/tool and normalized target class;
- whether the action remains inside the scope of the user's current request;
- the policy/confirmation decision and reason.

#### Confirmation policy — avoid both silent execution and consent spam

Direct user intent **may authorize ordinary, non-destructive read/navigation actions within the exact requested scope without a second confirmation**.

Examples that should not automatically require another prompt when directly and unambiguously requested:

- "Open Railway and check the latest deployment."
- "Open the PDF I downloaded earlier."
- "Go back to the page I was looking at."
- "Show me the files in Downloads."

A logged-in/authenticated browser session by itself does **not** make every read/navigation action sensitive.

Explicit user confirmation is required when at least one of these is true:

- the action is materially induced by untrusted observed content rather than direct user intent;
- the action expands beyond the scope of the trusted user request;
- the action reads credential-adjacent or unusually sensitive/private material not clearly requested;
- the action is destructive or difficult to reverse;
- the action sends/publishes/submits information to another party;
- the action changes account, billing, security, permission, identity, deployment, or other consequential authenticated state;
- the action initiates a purchase/payment or other material external commitment;
- the policy layer cannot establish that the requested target/action is the same one the user authorized.

If the current trusted user turn itself explicitly and unambiguously requests the consequential action, preserve the existing permission/policy rules for whether an additional confirmation is still required; do not add a blanket second confirmation solely because authentication is present.

Calls whose provenance is untrusted content must never inherit authority merely because that content is visible inside an authenticated page.

Permission/confirmation decisions remain explicit timeline state.

#### Required provenance classes

Use a small stable enum or equivalent normalized representation:

- `direct_user` — action follows directly from the current trusted user turn;
- `observed_untrusted` — action is materially suggested/induced by webpage, document, screen, email, or other observed content;
- `delegated_result` — action originates from a delegated worker/subagent result;
- `mixed` — more than one source materially contributed;
- `unknown` — provenance cannot be established; fail closed for side effects.

Do not infer `direct_user` from semantic similarity alone. It must be bound to an authenticated/trusted turn.

### Keep secrets out of model-visible payloads

Do not place into prompts, timeline summaries, routine telemetry, or speech:

- passwords;
- auth tokens;
- raw cookies;
- API keys;
- private keys;
- browser credential databases.

Local bridges may use existing authenticated OS/browser state without exposing the credential material itself.

### Destructive actions

For actions such as deletion, overwrite, mass move, account changes, or other materially destructive operations:

- preserve the existing permission/policy system;
- make the requested action specific;
- avoid ambiguous broad grants;
- make the resulting action auditable.

Do not invent a new permission subsystem in this task.

---

## Execution contract

Every concrete action adapter should normalize around a small common lifecycle rather than inventing custom UI semantics.

At minimum preserve:

- action/tool name;
- correlation/task ID;
- request timestamp;
- start timestamp;
- terminal status;
- concise result metadata;
- error category when failed;
- whether user permission was required;
- enough target metadata to diagnose the action without logging secrets.

Expected lifecycle:

```text
action.requested
      ↓
action.started
      ↓
action.completed
   or action.failed
      ↓
verification.requested
      ↓
verification.passed
   or verification.failed
```

Use existing timeline/event infrastructure where possible.

Do not add a second audit log if the timeline/Host telemetry can represent the same fact.

---

## Adapter requirements

### A. Filesystem

Implement the minimum safe primitives needed for ordinary personal-computer tasks.

At minimum investigate:

- list directory;
- locate file(s) by bounded criteria;
- stat/read metadata;
- create folder;
- move;
- rename;
- open/reveal file;
- optionally copy if the existing architecture supports it cleanly.

Requirements:

- operate on the user's real filesystem;
- normalize/resolve paths safely;
- reject traversal or malformed paths where relevant;
- preserve clear error messages;
- avoid reading file contents unless the task actually requires it;
- no silent destructive overwrite.

### B. Apps and processes

At minimum investigate:

- launch application;
- focus/activate application;
- open file with default application;
- open URL;
- inspect whether an app/process is running;
- quit an application where explicitly requested.

Keep platform-specific implementation behind a small adapter.

Do not leak macOS-specific semantics into core GNSIS reasoning/timeline contracts.

### C. Shell/process actions

Use shell/process execution only where it materially expands useful local control.

Requirements:

- bounded command execution;
- explicit working directory;
- timeout/cancellation;
- normalized stdout/stderr/exit status;
- no shell string concatenation for structured operations when direct APIs exist;
- permission/policy integration for risky actions;
- no secret dumping into logs.

A shell should not replace ordinary filesystem APIs.

### D. Existing browser control

Prototype the smallest local bridge that can work with the user's real browser session.

Investigate:

- current tab discovery;
- tab selection;
- navigation;
- click/type/scroll;
- DOM/accessibility-assisted targeting where available;
- download initiation;
- page-state/result reporting.

Do not require the model to receive the full browser profile or credential store.

If an existing browser-session bridge is not viable, document the exact blocker before falling back to isolated browser control.

### E. Generic desktop UI fallback

Provide a generic interaction path for applications that do not expose a better direct integration.

Requirements:

- current-screen observation remains authoritative;
- action output is timestamped/correlated;
- GNSIS observes after the action;
- failed/ambiguous outcomes do not get silently marked complete;
- avoid a separate screenshot-memory subsystem.

---

## Verification and recovery

This is part of the implementation, not a future enhancement.

A completed tool call is not sufficient proof.

For actions with an observable UI result:

1. record the expected visible outcome;
2. perform the action;
3. allow the current visual stream to update;
4. inspect the current/recent visual state;
5. determine whether the expected state occurred;
6. if not, retry only when the failure mode is understood and bounded;
7. otherwise surface a blocker or switch to an appropriate fallback.

Do not build an infinite retry loop.

Keep retries bounded and observable.

### Verification should be selective

Not every low-level filesystem mutation needs computer vision.

Examples:

- an OS-level successful rename plus a verified filesystem stat may be enough;
- opening the renamed file should normally be visually verified if the user's goal involves the opened application;
- browser navigation should generally be verified from browser/page state and/or current vision;
- UI clicking must be followed by state verification.

Use the strongest available ground truth, not vision for everything.

---

## Telemetry and measurement requirements

Computer-use telemetry must make it possible to distinguish:

- model decision/routing failure;
- capability negotiation/schema mismatch;
- Host routing failure;
- trusted-runtime/session rejection;
- provenance/intent-binding failure;
- confirmation required / allowed / denied;
- permission/TCC failure;
- adapter failure;
- OS/platform failure;
- browser-control failure;
- UI navigation failure;
- executor success but verification failure;
- bounded retry/fallback behavior;
- final completion.

Reuse the packaged Host persistent log and shared timeline rather than adding a competing audit system.

Do not log raw screen frames, microphone payloads, passwords, cookies, tokens, private keys, browser credential stores, or full private documents for routine diagnostics.

High-frequency telemetry must not synchronously block the realtime path.

### Required correlation fields

Every externally executable action must be reconstructable across runtime and Host telemetry.

Record, using existing IDs where possible:

- `session_id`;
- trusted `turn_id` / turn receipt identifier when applicable;
- unique external `call_id`;
- task/delegation ID when applicable;
- tool/capability name;
- capability manifest/schema version and stable hash/identity;
- normalized target class: filesystem, app, browser, generic_ui, shell, delegated;
- provenance class: `direct_user`, `observed_untrusted`, `delegated_result`, `mixed`, or `unknown`;
- policy decision: execute / confirm / refuse;
- policy reason;
- confirmation request ID and allow/deny result when confirmation occurs;
- executor/adapter selected;
- verification method selected;
- retry/fallback attempt number;
- terminal outcome.

Never place secret target data into these fields merely to make telemetry easier to read.

### Required timestamps

Use a consistent monotonic/comparable clock per process plus enough wall-clock correlation to align Host/runtime logs.

Capture at minimum:

- model external-tool call emitted;
- runtime call accepted/routed;
- Host call received;
- confirmation requested, when applicable;
- confirmation resolved, when applicable;
- action execution started;
- action execution finished;
- tool response sent by Host;
- tool response received/injected by runtime;
- verification requested;
- verification completed;
- retry/fallback started, when applicable;
- final goal completion/failure.

Derive separately:

- model -> Host routing latency;
- confirmation wait latency;
- executor latency;
- Host -> model response latency;
- verification latency;
- recovery/fallback latency;
- end-to-end action latency;
- end-to-end task latency.

Do not hide confirmation wait time inside executor latency.

### Host realtime correctness measurements

The five verified Host bugs above require their own acceptance evidence before computer-use baselines are trusted.

#### Socket error/reconnect

Record:

- socket/channel: duplex or screen;
- normalized error category;
- connection state when error occurred;
- whether Electron main remained alive;
- reconnect attempt number and delay;
- reconnect success/failure;
- time error -> recovered connection.

Acceptance:

- DNS/refused-upgrade/socket errors do not crash Electron main;
- errors are observable;
- bounded reconnect remains authoritative;
- no duplicate active sockets appear after recovery.

#### Playback queue continuity

For each output epoch/playback:

- chunk sequence/index if available;
- scheduled start time;
- scheduled duration/end time;
- queue depth in milliseconds;
- playback actual-start ACK;
- playback completion/cancellation.

Acceptance:

- scheduled chunks are monotonic;
- normal contiguous streaming does not overlap chunks;
- no negative scheduling gap is produced by immediate `src.start()`;
- epoch/cancel reset does not allow an old `nextStartTime` to contaminate later playback.

#### Playback cancellation / barge-in

Record:

- daemon cancellation control received time;
- playback/output epoch;
- local stop requested time;
- authoritative `playback.cancelled` time;
- queued chunks invalidated;
- stale chunks/audio started after cancellation.

Derive cancel-control -> local-stop latency.

Acceptance:

- stale chunks/audio after authoritative cancellation = **0**;
- cancelled output epoch can never later emit completed as if fully heard;
- new output is not blocked behind cancelled buffered speech.

#### Resampler accuracy

For each representative input sample rate, including 44.1 kHz:

- input frames;
- input sample rate;
- output frames;
- target sample rate;
- expected output-frame ratio;
- cumulative phase/error over a sustained run;
- sample-count/timestamp continuity.

Acceptance:

- fractional phase persists across worklet callbacks;
- long-run output rate remains aligned to 16 kHz rather than callback-local rounding;
- cumulative sample error stays bounded rather than growing linearly with callback count.

Do not validate this from one 128-frame callback only; use a sustained synthetic run long enough to expose drift.

#### Mic mute/unmute session integrity

Record:

- mute request;
- local tracks stopped;
- whether session/duplex socket remained alive;
- unmute request;
- tracks reacquired;
- first post-unmute audio frame sent;
- first post-unmute audio frame accepted/consumed where observable;
- UI mic state transitions.

Acceptance:

- muting does not silently send terminal session `stop` unless the user actually ends the call;
- unmute never sends into a dead session;
- UI reports mic on only after the capture + live transport path is usable;
- repeated mute/unmute cycles do not create duplicate capture streams or sockets.

### Correctness counters

Track at minimum:

- calls emitted;
- calls executed;
- calls refused by policy;
- calls requiring confirmation;
- confirmations allowed/denied;
- unknown/unadvertised tool rejections;
- schema/capability mismatches;
- untrusted/unauthenticated runtime rejections;
- stale/replayed call rejections;
- duplicate side effects detected — target **zero**;
- permission-denied actions;
- Accessibility-denied actions;
- target-specific Automation-denied actions;
- browser attachment failures;
- executor failures;
- verification failures after executor success;
- retries;
- fallback activations;
- final completed/failed goals.

### Capability negotiation evidence

For every acceptance session, record:

- Host build/commit;
- runtime build/commit;
- protocol version;
- negotiated local capability manifest version/hash;
- tool names presented to the model;
- tool names actually executable by the Host;
- any tool omitted because of Host capability, platform, permission, or schema/token-budget constraints.

Acceptance fails if the model is shown a local side-effecting tool the active Host did not advertise as executable.

### macOS TCC evidence

For real-Mac acceptance, record separately:

- Screen Recording state;
- Accessibility state;
- whether the selected mechanism uses Apple Events;
- when Apple Events are used, concrete target application/bundle identity and observed Automation consent/result for that target;
- permission request timestamp;
- allow/deny result;
- whether relaunch was required;
- action result after grant;
- action result after denial/revocation.

Do not collapse Accessibility and Automation into one `desktop_permission` boolean.

### Measurement output

Each PR that changes execution or realtime Host behavior must append/generate a concise acceptance record containing:

| Field | Required |
|---|---|
| commit/build identity | yes |
| test environment | yes |
| capability manifest identity | when execution applies |
| scenarios run | yes |
| pass/fail per scenario | yes |
| routing latency | when execution applies |
| executor latency | when execution applies |
| verification latency | when verification applies |
| total latency | yes |
| confirmation wait | separately when applicable |
| permission/TCC state | when relevant |
| playback cancel latency | when playback applies |
| resampler ratio/drift evidence | when audio capture changes |
| retries/fallbacks | yes |
| duplicate/replay count | yes |
| failure layer | for every failure |

Do not invent hard latency thresholds before a real baseline exists. First establish p50/p95 and observed tail behavior from representative real-device runs; then define evidence-backed healthy/warning bands.

---

## Implementation sequence

### Phase 0 — audit, no redesign

Before code changes:

1. inspect `ToolRegistry`, Host event paths, runtime external-tool handling, Harness client/bridge, provider registry, timeline, permissions, visual history, and current packaged reachability;
2. complete the gap matrix above;
3. trace one model-generated external tool call from model output to the client boundary and prove exactly where execution currently stops;
4. trace how `tool.response` returns to the live model;
5. inventory model-visible business-tool schemas and current schema/token budgets;
6. identify which actions fit direct local adapters versus delegated worker/Harness work;
7. verify how the Desktop trusts/authenticates its runtime before permitting local side effects;
8. verify the macOS Accessibility/native automation permission path;
9. verify the current existing-browser attachment mechanism and any extension/helper requirement;
10. prove whether the current Harness/provider stack can actually launch the intended open-ended desktop fallback;
11. identify any existing code already implementing candidate actions;
12. report the smallest implementation plan.

Do not propose a new general-purpose agent framework.

### PR H — realtime Desktop Host correctness gate

Fix the five verified Host correctness bugs before computer-use baseline measurement.

Scope only:

1. subscribe to duplex/screen WebSocket `error` events in `HostSession`, surface normalized transport telemetry/state, and preserve bounded reconnect without Electron-main crash;
2. serialize Talker/audio playback chunks with an accumulated `nextStartTime`, reset safely on epoch/cancel;
3. make daemon `playback.cancel` authoritative locally: stop current playback, discard stale queued chunks, emit `playback.cancelled`;
4. carry fractional resampler phase across worklet `process()` calls and prove sustained 16 kHz output ratio;
5. make the mic button a real mute/unmute lifecycle or explicitly reconnect before restart — never claim mic-on against a dead session.

Required automated tests:

- duplex socket error does not become an uncaught exception;
- screen socket error does not become an uncaught exception;
- reconnect remains bounded and no duplicate socket is created;
- two or more streaming playback chunks schedule serially without overlap;
- cancellation clears/stops current + queued stale playback and reports `playback.cancelled`;
- old epoch playback cannot resume after cancellation;
- sustained 44.1 kHz -> 16 kHz resampler test proves bounded cumulative error;
- mute does not terminate the live session;
- unmute resumes a usable capture/transport path;
- repeated mute/unmute does not duplicate tracks/sockets.

Preserve the already-fixed #106 preload and #111 token-gated screen behavior; include regression tests where practical rather than changing their architecture.

Stop after PR H is opened and report measurements required by **Host realtime correctness measurements**.

### PR 0 — local action broker + capability contract

Before adding filesystem/browser/UI adapters, close the authoritative local-tool transport.

Target:

- model-generated external/business `tool.call` reaches the owning Desktop Host;
- Host validates the negotiated tool name/schema;
- Host executes through `ToolRegistry`;
- result/error returns as correlated `tool.response`;
- runtime feeds the response back into the same live model session;
- duplicate/replayed calls cannot execute twice;
- disconnect/cancel behavior is deterministic;
- Host and runtime negotiate/version the local tool capability catalog;
- side-effecting local execution only runs for a trusted runtime/session.

Tests must prove one harmless deterministic local fixture tool end-to-end before real OS adapters are added.

Stop after PR 0 is opened and report the proven call/response path and remaining platform dependencies.

### PR A — deterministic local primitives

Wire the first concrete local actions through existing seams.

Target:

- filesystem;
- app/process launch/open;
- the minimum action lifecycle/telemetry required to diagnose them.

Tests must cover:

- valid action;
- invalid arguments;
- missing target;
- permission/policy path where relevant;
- cancellation/timeout where relevant;
- normalized result shape;
- no duplicate execution.

Stop after PR A is opened and report exactly what became callable.

### PR B — existing-browser bridge

Add/control the user's existing browser session where viable.

Target:

- discover/select active tab/session;
- navigate;
- interact;
- return normalized result metadata;
- preserve existing authentication without exposing credentials.

If the existing-session approach hits a real platform blocker, stop and document the blocker before substituting isolated Chromium.

Stop after PR B is opened.

### PR C — generic desktop fallback

Add the smallest generic desktop control surface needed for applications without direct adapters.

Target:

- macOS Accessibility preflight/grant/deny/revoke handling;
- native helper/bridge if required;
- focus;
- click;
- type;
- scroll;
- keyboard actions;
- correlation + telemetry;
- no brittle hard-coded workflow scripts.

macOS consent is required work in this PR, not a follow-up:

- cross-application focus/click/typing through accessibility/native control requires **Accessibility** trust;
- Apple-Events-driven control requires **Automation** consent for the concrete target application; do not model Automation as a single global Boolean;
- extend the Host permission model with Accessibility status/request handling and target-aware Automation request/result reporting for the native mechanism actually selected;
- include `NSAppleEventsUsageDescription` packaging metadata if Apple Events are used;
- an action lacking required consent fails with permission-denied telemetry — never a silent no-op and never an implicit grant;
- telemetry identifies whether failure was Accessibility, Automation for target app X, unsupported mechanism, or ordinary executor/navigation failure;
- real-device acceptance exercises allow and deny/revoke paths and records any macOS-required relaunch behavior;
- packaging metadata and real-device acceptance notes are part of the PR.

Do not add another visual-memory system.

Stop after PR C is opened.

### PR D — delegated/open-ended fallback activation

Prove and wire the launch path for the open-ended navigator only after deterministic/browser/UI primitives exist.

Target:

- an unfamiliar multi-step task can be started through the existing task/provider system;
- use OpenHands or another selected navigator only as the fallback executor;
- task progress, permissions, cancellation and terminal result flow through existing Gateway/Harness/timeline/delivery contracts;
- no second planner, memory system, or user-facing assistant is introduced.

If the existing Harness can only observe/control tasks and cannot launch this backend, add the smallest provider/launch adapter rather than redesigning Harness.

Stop after PR D is opened.

### PR E — verification/recovery closure

Wire action outcomes back into the existing perception/timeline path so GNSIS can distinguish:

- action completed and goal visibly achieved;
- action completed but goal not achieved;
- action failed;
- verification ambiguous;
- fallback/retry required.

Keep retries bounded.

Do not create a second planner or second assistant.

Stop after PR E is opened.

---

## Testing and acceptance requirements

Automated tests should prove adapters and lifecycle behavior without pretending they are real-Mac acceptance.

Use:

- unit tests for validation and normalization;
- integration tests for model -> runtime -> Host -> ToolRegistry -> `tool.response` routing;
- replay/duplicate-call tests;
- capability-manifest mismatch tests;
- trusted-runtime/session rejection tests;
- provenance/policy tests for all normalized provenance classes;
- confirmation-required / allow / deny tests;
- fake/stub OS/browser adapters where CI cannot operate a real desktop;
- timeline tests for action lifecycle and permission handling;
- verification tests using controlled visual/result fixtures;
- the Host correctness tests specified in PR H.

### Minimum policy matrix

| Provenance / action | Expected default |
|---|---|
| direct user + ordinary read/navigation inside explicit scope | execute without redundant confirmation |
| direct user + destructive/high-impact action | permission/policy path; confirm when required |
| direct user + sensitive/credential-adjacent read not explicitly scoped | confirm/refuse |
| observed untrusted content + side effect | never silently execute |
| observed untrusted content + request to expose sensitive data | refuse/confirm per policy; never inherit page authority |
| mixed provenance with ambiguous scope | confirm or refuse |
| unknown provenance + side effect | fail closed |
| stale/replayed call | reject; zero duplicate side effect |

### Minimum routing acceptance

Before PR A adapters count as usable, prove:

1. model emits a harmless external tool call;
2. runtime exposes it to exactly the owning Host/session;
3. Host checks negotiated capability + provenance/policy;
4. ToolRegistry executes exactly once;
5. Host returns the correlated response;
6. runtime injects that response into the same live model session;
7. model continues from the tool result;
8. telemetry reconstructs all seven steps.

### Real-device acceptance

Real-device acceptance remains a separate user-run step.

Do not claim a real Mac/browser workflow passed because a Linux CI mock passed.

For each real-device scenario, preserve the measurement fields in **Telemetry and measurement requirements** and identify the exact failure layer if it fails.

At minimum, real-Mac acceptance must eventually cover:

- socket DNS/refused-upgrade failure with Electron main surviving and bounded reconnect;
- continuous streamed speech with no chunk overlap;
- barge-in/daemon cancellation with zero stale playback after cancel;
- sustained mic capture proving no resampler drift;
- repeated mic mute/unmute without session death;
- direct filesystem/app primitive;
- existing authenticated browser read/navigation within explicit user scope;
- browser state-changing action that exercises confirmation/policy where required;
- generic desktop action with Accessibility granted;
- the same generic desktop action with Accessibility denied/revoked;
- an Apple-Events-driven action, if used, against at least one concrete target app with Automation allowed;
- the same target-specific Automation path denied, if Apple Events are part of the implementation;
- one induced/untrusted-content tool-call attempt proving it cannot silently operate the machine;
- one stale/replayed call proving zero duplicate side effect;
- one executor-success / verification-failure case proving GNSIS does not falsely mark the goal complete;
- one bounded retry/fallback case.

---

## Out of scope

Do not include in this program:

- local MiniCPM-o / llama.cpp-omni inference;
- model retraining;
- quantization;
- replacing the realtime provider;
- replacing Electron with Tauri;
- redesigning GNSIS memory;
- replacing the shared timeline;
- another delivery channel;
- a second vision model as the default verification system;
- generalized cloud computer-use infrastructure;
- speculative autonomous behavior unrelated to personal desktop use.

---

## Definition of done

This program is complete when:

1. current GNSIS execution architecture remains intact;
2. the two already-resolved desktop regressions (#106 preload ESM and #111 token-gated screen open) remain fixed;
3. WebSocket errors cannot crash Electron main and bounded reconnect is measurable;
4. streaming playback does not overlap chunks;
5. daemon cancellation produces authoritative local cancellation with zero stale playback after cancel;
6. sustained mic resampling remains aligned to the intended 16 kHz rate without cumulative callback-local drift;
7. mic mute/unmute preserves or correctly re-establishes a usable live session and never reports false mic-on state;
8. the live model can invoke a negotiated local Host tool and receive its response end-to-end;
9. local-tool calls are bound to a trusted runtime/session and cannot replay into duplicate side effects;
10. Host capabilities and model-visible tool schemas stay synchronized within the realtime schema budget;
11. deterministic filesystem/app actions are concretely callable;
12. ordinary local actions do not require a background subagent;
13. existing browser-session control is concretely callable or has a proven blocker with a documented fallback;
14. macOS Accessibility/native-control permission is handled explicitly, and target-specific Automation consent is handled when Apple Events are used;
15. generic desktop UI control exists for cases with no better adapter;
16. the selected open-ended fallback can actually be launched through the existing task/provider system;
17. Harness remains the delegated/open-ended execution integration rather than becoming the path for every action;
18. permission and confirmation decisions remain explicit without forcing redundant confirmation for ordinary direct-user actions inside scope;
19. secrets remain outside model-visible context and routine telemetry;
20. actions emit attributable lifecycle/provenance state;
21. GNSIS can observe and verify important action outcomes using the existing perception path;
22. failed verification can trigger bounded recovery/fallback rather than silent success;
23. automated tests prove Host correctness, adapter routing, provenance/policy, replay protection, and lifecycle behavior;
24. acceptance telemetry reconstructs every action from trusted intent through execution, response injection, verification and final outcome;
25. real-device acceptance records the required build, capability, timing, policy, TCC, audio/playback and failure-layer evidence;
26. real-device acceptance can exercise the complete:

```text
HEAR / SEE
   ↓
UNDERSTAND
   ↓
ACT
   ↓
SEE
   ↓
VERIFY / ADAPT
   ↓
COMPLETE
```

The objective is not to design “computer use.”

The architecture already exists.

The objective is to finish attaching the real local actions to it and measure the complete loop accurately.
