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


class DockerSandboxCommandTests(unittest.TestCase):
    """Regression: the runner must be invoked once, not doubled by the image
    ENTRYPOINT (docker appends `docker run` CMD to an image's ENTRYPOINT)."""

    def _cmd(self):
        from gnsis.orchestration.engine import Workspace
        from gnsis.service.sandbox import DockerEngine

        ws = Workspace(path="/tmp/ws", repo="o/r", base_branch="main")
        return DockerEngine(inner_engine="mock", image="img:latest")._docker_command(ws)

    def test_entrypoint_is_overridden(self):
        cmd = self._cmd()
        self.assertIn("--entrypoint", cmd)
        self.assertEqual(cmd[cmd.index("--entrypoint") + 1], "python")

    def test_runner_module_invoked_exactly_once(self):
        cmd = self._cmd()
        self.assertEqual(cmd.count("gnsis.service.runner"), 1)
        # `-m <module>`, not `python -m <module>` after the image (that would double up)
        i = cmd.index("gnsis.service.runner")
        self.assertEqual(cmd[i - 1], "-m")
        self.assertEqual(cmd[i - 2], "img:latest")


if __name__ == "__main__":
    unittest.main()
