"""The engine registry and that engines build without their heavy deps.

Both real engines import their SDK/CLI lazily (Claude SDK inside the run; the
OpenHands CLI via subprocess), so constructing them must work offline — only
actually running them needs the dependency. That keeps the swappable seam cheap.
"""

import unittest

from gnsis.engines import get_engine
from gnsis.orchestration.engine import MockEngine


class EngineRegistryTests(unittest.TestCase):
    def test_mock_engine(self):
        self.assertIsInstance(get_engine("mock"), MockEngine)

    def test_claude_engine_builds_without_sdk(self):
        self.assertEqual(get_engine("claude").name, "claude")

    def test_openhands_engine_builds_without_dep(self):
        self.assertEqual(get_engine("openhands").name, "openhands")

    def test_unknown_engine_raises(self):
        with self.assertRaises(ValueError):
            get_engine("nope")


if __name__ == "__main__":
    unittest.main()
