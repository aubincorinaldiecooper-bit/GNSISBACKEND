# Provenance of the live runtime

`gander/` is a vendored copy of the runtime that serves the phone page at `/live`, so that this repository's production build depends on no other repository.

## Where it came from

- Upstream project: `Omni-Interaction-Gander/Omni-Interaction-Agent`
- Fork the runtime was first imported from: `aubincorinaldiecooper-bit/Omni-Interaction-Agent`, commit `e5f3f755c39dcda30ab0638ce332d3b2dca6526e`
- Import scope: `gander_runtime/`, `minicpm_ft/mcpmft/`, their tests and package metadata, and the Apache-2.0 licence. Deliberately excluded: docs, report/images, training/examples, caches/bytecode.
- Then developed inside `aubincorinaldiecooper-bit/CLIPIT` under `gander/`: the Ornith provider (`gander_runtime/gander_runtime/providers/ornith.py`, registered alongside the upstream Codex provider), the lifecycle hardening, the phone page and its brand system, haptics, and the semantic haptic output channel.
- Moved here from CLIPIT at commit `2935db1`, the head of CLIPIT #159 stacked on CLIPIT #158, and adapted to this repository: the Modal app is `gnsis-live` and the deployment configuration is `configs/gnsis-live.yaml`. From that point this repository is the only source of truth for the runtime; CLIPIT's copy is removed.

## Syncing upstream later

Compare a future upstream/fork revision against the imported commit above, review runtime changes, then intentionally port the relevant changes into `gander/`. Do not make production builds clone the external repository.
