"""Tests for the long-term memory adapter interface.

We ship only the interface and a no-op default today; SimpleMem is a deliberate
stub. These tests pin that contract so future providers conform to it.
"""

import unittest

from gnsis.memory import (
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


if __name__ == "__main__":
    unittest.main()
