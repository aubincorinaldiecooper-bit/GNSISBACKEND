"""Public-beta remote execution — the GitHub Actions control plane.

Everything that makes a user coding job run *remotely* in the fixed executor
workflow (and never inside the Railway API or Celery worker) lives here:

* :mod:`.models` — execution status/failure vocabulary and plain record shapes.
* :mod:`.store` — the durable ``execution_runs`` boundary (atomic nonce consume,
  hashed-token binding, budget accounting).
* :mod:`.tokens` — random, hashed, single-purpose executor tokens.
* :mod:`.oidc` — cryptographic verification of GitHub Actions OIDC identities.
* :mod:`.installation` — automatic executor GitHub App installation resolution.
* :mod:`.dispatch` — scope-narrowed workflow dispatch (job_id + nonce only).
* :mod:`.source` — single-use, SHA-bound immutable source delivery.
* :mod:`.gateway` — the restricted OpenAI-compatible model gateway.
* :mod:`.validation` — server-side patch/output validation against clean source.
* :mod:`.callbacks` — authenticated events/complete/failed handling.
* :mod:`.reconcile` — polling reconciliation (the source of truth).
* :mod:`.api` — the ``/internal/executor/*`` and ``/internal/model/*`` router.
"""

from __future__ import annotations
