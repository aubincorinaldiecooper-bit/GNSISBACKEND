# AGENTS.md — GNSIS Runtime

This subtree implements the GNSIS daemon/runtime. In addition to the repository-root brief, computer-use work here is governed by `docs/computer-use/AGENTS.md` — treat that document as binding for any task under `runtime/**`. Its non-negotiable rules restated for this tree:

## Binding rules for `runtime/**`

- **External tool calls must be answerable.** The runtime emits external tool calls and then waits for a correlated client `tool.response`. Any change to the tool-call surface must preserve that request/response contract — a call with no responder path leaves the model blocked.
- **Bind actions to authenticated user intent.** Untrusted content (web pages, documents) can induce model-generated tool calls. External-tool execution must carry origin/provenance so sensitive reads and authenticated-session actions can require explicit user confirmation; the trusted `turn.final` binding used by task tools is the reference behavior.
- **Permissions are explicit state, never implicit grants.** A permission request produces a timeline event and an explicit allow/deny decision — never an automatic grant.
- **Keep secrets out of model-visible payloads.** No passwords, tokens, cookies, API keys, private keys, or credential stores in prompts, timeline summaries, routine telemetry, or speech.
- **Timeline is the audit surface.** Action lifecycle events (requested/started/completed/failed/verification) use the existing timeline/event infrastructure; do not add a second audit log.
- **Bounded visual history only.** Recent visual state stays inside the timestamp-indexed bounded history — no unbounded retention, no filesystem screen archive, no parallel visual memory beside Omni-SimpleMem.
- **Sequencing and testing.** Follow the brief's phase order and testing requirements; runtime changes stay consistent with the desktop Host's `AGENTS.md` contract.
