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

Also confirm the important current limitation:

> The packaged Desktop `ToolRegistry` currently exposes only `internet_search`.

That is the known last-mile gap. Do not mistake it for an absence of execution architecture.

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

1. inspect `ToolRegistry`, Host event paths, Harness client/bridge, timeline, permissions, visual history, and current packaged reachability;
2. complete the gap matrix above;
3. identify which actions fit direct local adapters versus Harness-delegated work;
4. identify any existing code already implementing candidate actions;
5. report the smallest implementation plan.

Do not propose a new general-purpose agent framework.

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

- focus;
- click;
- type;
- scroll;
- keyboard actions;
- correlation + telemetry;
- no brittle hard-coded workflow scripts.

Do not add another visual-memory system.

Stop after PR C is opened.

### PR D — verification/recovery closure

Wire action outcomes back into the existing perception/timeline path so GNSIS can distinguish:

- action completed and goal visibly achieved;
- action completed but goal not achieved;
- action failed;
- verification ambiguous;
- fallback/retry required.

Keep retries bounded.

Do not create a second planner or second assistant.

Stop after PR D is opened.

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
2. deterministic filesystem/app actions are concretely callable;
3. ordinary local actions do not require a background subagent;
4. existing browser-session control is concretely callable or has a proven blocker with a documented fallback;
5. generic desktop UI control exists for cases with no better adapter;
6. Harness remains the delegated/open-ended execution backend rather than becoming the path for every action;
7. permission requests remain explicit;
8. secrets remain outside model-visible context and routine telemetry;
9. actions emit attributable lifecycle state;
10. GNSIS can observe and verify important action outcomes using the existing perception path;
11. failed verification can trigger bounded recovery/fallback rather than silent success;
12. automated tests prove adapter and lifecycle behavior;
13. real-device acceptance can exercise the complete:

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
