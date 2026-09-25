# AGENTS.md — GNSIS Runtime

This subtree implements the GNSIS daemon/runtime. In addition to the repository-root brief, computer-use work here is governed by `docs/computer-use/AGENTS.md` — treat that document as binding for any task under `runtime/**`. Its non-negotiable rules restated for this tree:

## Binding rules for `runtime/**`

- **External tool calls must be answerable.** The runtime emits external tool calls and then waits for a correlated client `tool.response`. Any change to the tool-call surface must preserve that request/response contract — a call with no responder path leaves the model blocked.
- **Bind actions to authenticated user intent.** Untrusted content (web pages, documents) can induce model-generated tool calls. Every external call carries normalized provenance bound to trusted `turn.final` state. Direct-user ordinary read/navigation inside explicit scope may execute without redundant confirmation; untrusted, ambiguous, out-of-scope or consequential calls follow the governing brief's confirmation/refusal policy.
- **Permissions are explicit state, never implicit grants.** A permission request produces a timeline event and an explicit allow/deny decision — never an automatic grant.
- **Keep secrets out of model-visible payloads.** No passwords, tokens, cookies, API keys, private keys, or credential stores in prompts, timeline summaries, routine telemetry, or speech.
- **Timeline is the audit surface.** Action lifecycle events (requested/started/completed/failed/verification) use the existing timeline/event infrastructure; include session/turn/call correlation, provenance, policy decision/reason, capability-manifest identity, timestamps, retry/fallback and terminal outcome. Do not add a second audit log.
- **Bounded visual history only.** Recent visual state stays inside the timestamp-indexed bounded history — no unbounded retention, no filesystem screen archive, no parallel visual memory beside Omni-SimpleMem.
- **Sequencing and testing.** Follow Phase 0 → PR H → PR 0 → PR A → PR B → PR C → PR D → PR E. PR 0 must prove model→runtime→owning Host→ToolRegistry→correlated `tool.response`→same live model session before local adapters count as callable. Runtime changes stay consistent with the Desktop Host contract and the governing measurement matrix.

- **Capability negotiation is measurable.** The runtime must not present a side-effecting local tool the active Host did not advertise. Record protocol/build identity, negotiated manifest version/hash, model-visible tool names, Host-executable names and omissions due to platform, permission or schema/token budget.
- **Replay safety is mandatory.** Stale/replayed external calls are rejected and the target duplicate-side-effect count is zero.
