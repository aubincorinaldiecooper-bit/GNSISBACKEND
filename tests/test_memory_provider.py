"""Tests for the long-term memory adapter interface.

We ship only the interface and a no-op default today; SimpleMem is a deliberate
stub. These tests pin that contract so future providers conform to it.
"""

import unittest

from gnsis.memory import (
    InMemoryMemoryProvider,
    MemoryProvider,
    MemoryRecord,
    NullMemoryProvider,
    SimpleMemProvider,
)


class MemoryProviderTests(unittest.TestCase):
    def test_null_provider_is_a_provider(self):
        self.assertIsInstance(NullMemoryProvider(), MemoryProvider)

    def test_null_provider_drops_writes_and_reads_empty(self):
        provider = NullMemoryProvider()
        rec = MemoryRecord(repo="o/r", content="prefer tabs", approved=True)
        self.assertIsNone(provider.write(rec))
        self.assertEqual(provider.search("o/r", "tabs"), [])
        self.assertEqual(provider.recent("o/r"), [])

    def test_record_defaults_to_unapproved(self):
        rec = MemoryRecord(repo="o/r", content="x")
        self.assertFalse(rec.approved)
        self.assertTrue(rec.created_at)

    def test_simplemem_is_not_implemented_yet(self):
        with self.assertRaises(NotImplementedError):
            SimpleMemProvider()


class InMemoryProviderTests(unittest.TestCase):
    def setUp(self):
        self.mem = InMemoryMemoryProvider()

    def test_approval_gates_writes(self):
        self.assertIsNone(
            self.mem.write(MemoryRecord(repo="o/r", content="unapproved"))
        )
        self.assertEqual(self.mem.recent("o/r"), [])

        rec = MemoryRecord(repo="o/r", content="prefer pytest", approved=True)
        self.assertIsNotNone(self.mem.write(rec))
        self.assertEqual(len(self.mem.recent("o/r")), 1)

    def test_reads_are_repo_scoped(self):
        self.mem.write(MemoryRecord(repo="o/a", content="alpha note", approved=True))
        self.mem.write(MemoryRecord(repo="o/b", content="beta note", approved=True))
        self.assertEqual(len(self.mem.recent("o/a")), 1)
        self.assertEqual(self.mem.recent("o/a")[0].content, "alpha note")
        self.assertEqual(self.mem.search("o/b", "beta")[0].content, "beta note")
        self.assertEqual(self.mem.search("o/a", "beta"), [])


if __name__ == "__main__":
    unittest.main()
