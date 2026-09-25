# AGENTS.md — Desktop Host

This subtree implements the GNSIS Desktop Host. In addition to the repository-root brief, computer-use work here is governed by `docs/computer-use/AGENTS.md` — treat that document as binding for any task under `desktop/**`. Its non-negotiable rules restated for this tree:

## Binding rules for `desktop/**`

- **One persistent visual sense.** All perception stills derive from the live screen/camera stream; never reintroduce a screenshot-based perception path. Generic desktop UI observation uses the already-active capture lifecycle.
- **No second execution stack.** Local actions plug into `ToolRegistry` and the existing Host/daemon seams — do not build a parallel action framework beside them.
- **Bidirectional tool routing is required, not optional.** When the runtime emits an external tool call and awaits a client `tool.response`, the Host must execute the call through `ToolRegistry` and return a correlated response. A tool that is registered but unreachable from the model is not done.
- **Bind actions to authenticated user intent.** Model-generated tool calls can be induced by untrusted text on screen or in a document. Sensitive reads and authenticated-session actions require origin/provenance checks and explicit user confirmation — approval-when-a-backend-asks alone is not sufficient.
- **Permissions are explicit state, never implicit grants.** Microphone, camera, screen, Accessibility, and Automation/Apple-Events consent are each surfaced as explicit status/request/decision. macOS Accessibility and Automation consent must include status checks, the system prompt path, packaging metadata, defined denial behavior (fail the action with permission-denied telemetry — never a silent no-op), and real-device acceptance.
- **Sequencing.** Follow the brief's Phase 0 → PR A → PR B → PR C → PR D order; do not collapse phases or implement later PRs early.
- **Testing.** Adapter and lifecycle tests must prove valid action, invalid arguments, missing target, permission/policy path, cancellation/timeout, normalized result shape, and no duplicate execution — without pretending to be real-Mac acceptance.
