# Runtime provenance

The realtime multimodal runtime under `runtime/` is vendored into GNSIS so production does not depend on a second source checkout.

- The imported runtime is distributed under Apache-2.0; its license is retained at `runtime/LICENSE`.
- The initial imported source snapshot is pinned in Git history.
- GNSIS owns all product-facing naming, deployment configuration, live-surface behavior, tests, and subsequent runtime modifications in this repository.
- Future upstream synchronization should be reviewed as source changes, not copied as a second product identity.

The current repository is the source of truth for the GNSIS realtime runtime.

## GNSIS 0.5 local model

- OmniJev source contract: `tinnel123666888/OmniJev` commit
  `14dbec4f71e194852c8d7b88ab36ef639493f400`, Apache-2.0.
- Released local checkpoint: `tinnel123/OmniJev-0.8B` v1.1, Apache-2.0.
- Shared backbone: `Qwen/Qwen3.5-0.8B`, subject to the model repository's
  own license.
- GNSIS loads upstream OmniJev from an explicit local source directory rather
  than copying its implementation into this repository.
- Local speech input: `faster-whisper` with `small.en`, CPU `int8` by default.
- Local speech output: `kittenml` 0.9.4 (KittenML/KittenTTS commit
  `8ed679c08e0976b236bd5d0dc4ec9ddd98b0da98`, code Apache-2.0) loading
  `KittenML/kitten-tts-2`. The weights use the Stellon Labs Community License:
  limited commercial use ends above USD 1,000,000 in annual revenue or total
  cumulative funding, and products using the weights carry attribution.
