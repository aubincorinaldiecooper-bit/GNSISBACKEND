import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from gnsis.memory import Memory, MemoryProvider, SimpleMemAdapter  # noqa: E402
from gnsis.resources import ResourceStore, ResourceStoreBackend  # noqa: E402


class PersistenceSeamTests(unittest.TestCase):
    def test_resource_store_satisfies_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsInstance(ResourceStore(tmp), ResourceStoreBackend)

    def test_memory_satisfies_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsInstance(Memory(tmp), MemoryProvider)

    def test_simplemem_is_a_provider_but_not_wired(self):
        adapter = SimpleMemAdapter()
        self.assertIsInstance(adapter, MemoryProvider)
        with self.assertRaises(NotImplementedError):
            adapter.remember({"x": 1})


if __name__ == "__main__":
    unittest.main()
