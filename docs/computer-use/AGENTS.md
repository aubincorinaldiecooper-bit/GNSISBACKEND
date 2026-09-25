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

On macOS, UI automation must account for Accessibility trust and, depending on the chosen native mechanism, any additional Automation/Apple Events prompts.

The generic desktop implementation must include:

- status/preflight for Accessibility trust;
- a clear user path to grant it;
- correct behavior when denied/revoked;
- relaunch/retry behavior where the OS requires it;
- a small native helper/bridge if Electron/Node cannot provide the required OS API directly;
- telemetry that distinguishes permission denial from navigation failure.

Do not report generic desktop control as available merely because screen capture works.

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

Required:

- every external tool call carries origin/provenance (which turn and which input — user speech, user text, or model output influenced by observed screen/page content — produced it), using the same trusted `turn.final` binding the task tools already rely on;
- sensitive reads (private files, browser session surfaces, credential-adjacent paths) and actions that use authenticated session state require explicit user confirmation before execution;
- calls whose provenance is untrusted content rather than direct user intent must not execute silently — they surface as a confirmation request or are refused;
- permission decisions remain explicit timeline state.

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

## Telemetry requirements

Computer-use telemetry must make it possible to distinguish:

- decision/routing failure;
- permission failure;
- adapter failure;
- OS/platform failure;
- browser-control failure;
- UI navigation failure;
- executor success but visual verification failure;
- recovery/fallback behavior;
- final completion.

Where the packaged Host persistent log exists, reuse it.

Where the shared timeline already records task state, reuse it.

Do not log raw screen frames, microphone payloads, passwords, cookies, tokens, or full private documents for routine diagnostics.

High-frequency events should not synchronously block the realtime path.

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

- cross-application focus/click/typing needs **Accessibility** trust, and Apple-Events-driven control needs **Automation** consent — the packaged Host currently models only microphone/camera/screen;
- extend the Host permission model with accessibility/automation status + request paths (Accessibility via the TCC trusted-process prompt, Automation via the Apple Events usage consent and `NSAppleEventsUsageDescription` packaging metadata);
- defined denial behavior: an action that lacks consent fails with permission-denied telemetry — never a silent no-op and never an implicit grant;
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

## Testing requirements

Automated tests should prove adapters and lifecycle behavior without pretending they are real-Mac acceptance.

Use:

- unit tests for validation and normalization;
- integration tests for Host/daemon action routing;
- fake/stub OS/browser adapters where CI cannot operate a real desktop;
- timeline tests for action lifecycle and permission handling;
- verification tests using controlled visual/result fixtures.

Real-device acceptance remains a separate user-run step.

Do not claim a real Mac/browser workflow passed because a Linux CI mock passed.

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
2. the live model can invoke a negotiated local Host tool and receive its response end-to-end;
3. local-tool calls are bound to a trusted runtime/session and cannot replay into duplicate side effects;
4. Host capabilities and model-visible tool schemas stay synchronized within the realtime schema budget;
5. deterministic filesystem/app actions are concretely callable;
6. ordinary local actions do not require a background subagent;
7. existing browser-session control is concretely callable or has a proven blocker with a documented fallback;
8. macOS Accessibility/native-control permission is handled explicitly and generic desktop UI control exists for cases with no better adapter;
9. the selected open-ended fallback can actually be launched through the existing task/provider system;
10. Harness remains the delegated/open-ended execution integration rather than becoming the path for every action;
11. permission requests remain explicit;
12. secrets remain outside model-visible context and routine telemetry;
13. actions emit attributable lifecycle state;
14. GNSIS can observe and verify important action outcomes using the existing perception path;
15. failed verification can trigger bounded recovery/fallback rather than silent success;
16. automated tests prove adapter and lifecycle behavior;
17. real-device acceptance can exercise the complete:

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

The objective is to finish attaching the real local actions to it.
