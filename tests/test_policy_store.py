"""Phase 3 — versioned Genesis/Ponytail policy stored as a durable resource."""

from __future__ import annotations

import hashlib
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from _authkit import fresh_sqlite_env  # noqa: E402


def _configure():
    fresh_sqlite_env()
    from gnsis.service import settings as sm

    sm._settings = None
    from gnsis.service.db import init_db

    init_db()


class PolicyStoreTests(unittest.TestCase):
    def setUp(self):
        _configure()
        from gnsis.service import policy_store as ps

        self.ps = ps
        self.store = ps.PostgresResourceStore()

    def test_seed_creates_v1_from_the_ladder(self):
        from gnsis.agent.policy import build_system_prompt

        pol = self.ps.seed_default_policy(self.store)
        self.assertEqual(pol.version, 1)
        self.assertEqual(pol.name, self.ps.POLICY_NAME)
        self.assertEqual(pol.content, build_system_prompt())
        # The pinned hash is a plain sha256 over the exact prompt bytes.
        self.assertEqual(
            pol.content_hash,
            hashlib.sha256(pol.content.encode("utf-8")).hexdigest(),
        )
        # It carries the Ponytail ladder, not the throwaway sandbox stub.
        self.assertIn("ladder", pol.content.lower())

    def test_seed_is_idempotent(self):
        a = self.ps.seed_default_policy(self.store)
        b = self.ps.seed_default_policy(self.store)
        self.assertEqual(a.version, 1)
        self.assertEqual(b.version, 1)
        history = self.store.history(self.ps.POLICY_KIND, self.ps.POLICY_NAME)
        self.assertEqual(len(history), 1)

    def test_resolve_active_seeds_when_missing(self):
        pol = self.ps.resolve_active_policy(self.store)
        self.assertEqual(pol.version, 1)

    def test_active_follows_head_but_history_is_retained(self):
        self.ps.seed_default_policy(self.store)
        # A human commits a v2 (a DSPy draft would NOT be auto-committed here).
        self.store.commit(
            self.ps.POLICY_KIND,
            self.ps.POLICY_NAME,
            build := "Genesis v2 policy body with an explicit ladder step.",
            message="manual promotion",
        )
        active = self.ps.resolve_active_policy(self.store)
        self.assertEqual(active.version, 2)
        self.assertEqual(active.content, build)

        # Every historical version stays reconstructable, unchanged.
        v1 = self.ps.get_policy_version(1, self.store)
        self.assertEqual(v1.version, 1)
        from gnsis.agent.policy import build_system_prompt

        self.assertEqual(v1.content, build_system_prompt())

    def test_get_missing_version_returns_none(self):
        self.ps.seed_default_policy(self.store)
        self.assertIsNone(self.ps.get_policy_version(99, self.store))

    def test_hash_is_verifiable_from_content_alone(self):
        pol = self.ps.resolve_active_policy(self.store)
        # Exactly what the executor does to verify the policy in transit.
        recomputed = hashlib.sha256(pol.content.encode("utf-8")).hexdigest()
        self.assertEqual(recomputed, pol.content_hash)


if __name__ == "__main__":
    unittest.main()
