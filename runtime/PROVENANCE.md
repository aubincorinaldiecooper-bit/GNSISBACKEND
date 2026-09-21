# Runtime provenance

The realtime multimodal runtime under `runtime/` is vendored into GNSIS so production does not depend on a second source checkout.

- The imported runtime is distributed under Apache-2.0; its license is retained at `runtime/LICENSE`.
- The initial imported source snapshot is pinned in Git history.
- GNSIS owns all product-facing naming, deployment configuration, live-surface behavior, tests, and subsequent runtime modifications in this repository.
- Future upstream synchronization should be reviewed as source changes, not copied as a second product identity.

The current repository is the source of truth for the GNSIS realtime runtime.
