# Provenance

The live perception runtime in this directory is vendored into GNSIS so production builds are self-contained and do not clone an external repository at startup.

## Source

- Upstream project: `Omni-Interaction-Agent`
- Imported revision: `e5f3f755c39dcda30ab0638ce332d3b2dca6526e`
- Imported scope: the realtime runtime, the MiniCPM fine-tuning/inference core, their tests, package metadata, and the Apache-2.0 license.
- GNSIS maintains all subsequent product, runtime, deployment, interface, privacy, haptics, and reliability changes in this repository.

## Updating

Compare a future upstream revision against the imported commit above, review the relevant runtime changes, and port them intentionally into `live/`. Production builds should remain self-contained.

The upstream license is preserved in `live/LICENSE`.
