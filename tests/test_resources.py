import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from gnsis.resources import ResourceStore, canonical_hash  # noqa: E402


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ResourceStore(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_canonical_hash_is_order_independent(self):
        self.assertEqual(canonical_hash({"a": 1, "b": 2}), canonical_hash({"b": 2, "a": 1}))
        self.assertNotEqual(canonical_hash("x"), canonical_hash("y"))

    def test_commit_creates_versions_with_lineage(self):
        v1 = self.store.commit("prompt", "p", "first", message="seed")
        v2 = self.store.commit("prompt", "p", "second", message="improve")
        self.assertEqual(v1.version, 1)
        self.assertIsNone(v1.parent_version)
        self.assertEqual(v2.version, 2)
        self.assertEqual(v2.parent_version, 1)
        self.assertEqual(self.store.head("prompt", "p").content, "second")
        self.assertEqual(len(self.store.history("prompt", "p")), 2)

    def test_persistence_round_trip(self):
        self.store.commit("prompt", "p", {"text": "hello"})
        reloaded = ResourceStore(self._tmp.name)
        self.assertEqual(reloaded.head("prompt", "p").content, {"text": "hello"})

    def test_rollback_is_append_only(self):
        self.store.commit("prompt", "p", "v1-content")
        self.store.commit("prompt", "p", "v2-content")
        rolled = self.store.rollback("prompt", "p", to_version=1)
        self.assertEqual(rolled.version, 3)
        self.assertEqual(rolled.content, "v1-content")
        # History preserved, not erased.
        self.assertEqual(len(self.store.history("prompt", "p")), 3)

    def test_lineage_walks_parents(self):
        self.store.commit("prompt", "p", "a")
        self.store.commit("prompt", "p", "b")
        self.store.commit("prompt", "p", "c")
        resource = self.store.load("prompt", "p")
        chain = [v.content for v in resource.lineage()]
        self.assertEqual(chain, ["a", "b", "c"])

    def test_delete(self):
        self.store.commit("prompt", "p", "x")
        self.assertTrue(self.store.delete("prompt", "p"))
        self.assertIsNone(self.store.head("prompt", "p"))
        self.assertFalse(self.store.delete("prompt", "p"))


if __name__ == "__main__":
    unittest.main()
