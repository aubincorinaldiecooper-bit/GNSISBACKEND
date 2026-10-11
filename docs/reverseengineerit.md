# REVERSEENGINEERIT

Parked feature branch. Not started; picked up after the current task queue.

## Goal

A GNSIS 1.0 desktop function, `REVERSEENGINEERIT`, that lets a person point
GNSIS at an app ("how does search work in this app?"), have agents inspect
the shipped app without its source, explain the feature with evidence, and
build their own version of that feature in their own project.

## Building block

[REA — Reverse Engineer Anything](https://github.com/morluto/rea)
(npm `rea-agents`, MIT, v6.4.0 when this branch was cut). One MCP server
plus CLI that inspects native binaries (via Hopper/Ghidra/IDA), Electron /
JavaScript apps, .NET assemblies, Android APKs, websites and HAR captures.
It runs locally and returns findings with evidence and stated unknowns.

Manual MCP registration from its docs:

```json
{ "command": "npx", "args": ["-y", "rea-agents@6.4.0", "mcp"] }
```

## Where it plugs in (no new subsystem)

- **Agent side:** the Codex worker (`runtime/gnsis_runtime/gnsis_runtime/providers/codex.py`,
  `_command`) already launches Codex with per-run MCP servers
  (`gnsis_context`, `gnsis_share`). REVERSEENGINEERIT adds REA as one more
  pinned `mcp_servers.rea` entry for runs of this kind only, plus REA's
  `reverse-engineer-anything` skill as the worker instructions.
- **Delivery:** results come back through the existing `share` tool and the
  delivery gate, like any other background task.
- **Desktop side:** the target app lives on the person's Mac and REA is
  local-only, so the analysis must run on the Mac, not on Modal. The
  desktop launches/owns the REA process through `ToolRegistry` /
  ActionBroker (`desktop/src/tools/registry.ts`, `desktop/src/host/actionBroker.ts`),
  never a second execution stack (`desktop/AGENTS.md`).

## Owner decisions (2026-10-11)

- Runs in the cloud, not on the person's Mac.
- Paid feature.
- "Deploy their own" = the person's own version, hosted by GNSIS.
- Developer-focused first (output is code + evidence a developer can
  judge; fits the existing GitHub developer access). A consumer version may
  come later on the same engine.

## What the cloud decision changes

- REA is designed as local-only, so the target must come to us: an
  uploaded artifact, a download URL, or a website URL, analysed inside an
  isolated per-run cloud sandbox with REA + the worker agent.
- Cloud-friendly targets: websites, Electron/JS, Android APKs, .NET,
  Linux/ELF (Ghidra headless). Native macOS apps: Hopper is macOS-first;
  Ghidra on Linux works with less depth.
- The desktop is no longer the execution place; the "Desktop side" note
  above only applies if a local mode is added later.
- Hosting by GNSIS puts the legal exposure on us: build an original
  implementation of the feature under the person's own branding, never
  host copied code, assets or trademarks of the target; record the
  person's confirmation that they may analyse the target for each run.

## Open questions before building

1. Production has no task layer yet: `runtime/configs/gnsis-voice.yaml` has
   `worker.provider: none`. REVERSEENGINEERIT needs delegation turned on
   first (Codex worker, or the Qwen Live Harness ACP path from `AGENTS.md`).
2. Decided: cloud. Still open: which sandbox (e.g. Modal sandbox per run)
   and how the target artifact is supplied.
3. Native targets need Hopper, Ghidra or IDA installed by the person; REA
   setup can offer Hopper with approval. Electron/JS, .NET and website
   analysis need only Node.js 22.19+.
4. Consent and scope: REA runs with the person's user permissions. Each run
   should name the target app and be approved by the person (ActionBroker
   policy), and output should be "build a similar feature", not copying the
   target's code or assets.
5. Decided: hosted by GNSIS. Still open: hosting target (Coolify, which
   already hosts the control plane, vs another platform), pricing/metering
   (reuse the existing usage ledger + spending limits).
