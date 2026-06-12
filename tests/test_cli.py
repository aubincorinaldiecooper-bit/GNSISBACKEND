import contextlib
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from gnsis import cli  # noqa: E402


def run_cli(argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.main(argv)
    return code, out.getvalue()


class CliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workdir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_version(self):
        code, output = run_cli(["version"])
        self.assertEqual(code, 0)
        self.assertIn("gnsis", output)

    def test_no_command_prints_help(self):
        code, output = run_cli([])
        self.assertEqual(code, 0)
        self.assertIn("usage", output.lower())

    def test_run_offline(self):
        code, output = run_cli(
            ["run", "--provider", "mock", "--workdir", self.workdir, "--task", "What is (12 + 30) * 2?"]
        )
        self.assertEqual(code, 0)
        self.assertIn("84", output)

    def test_evolve_offline(self):
        code, output = run_cli(
            ["evolve", "--provider", "mock", "--workdir", self.workdir, "--fresh"]
        )
        self.assertEqual(code, 0)
        self.assertIn("1.00", output)
        self.assertIn("→", output)

    def test_demo(self):
        code, output = run_cli(["demo", "--workdir", self.workdir])
        self.assertEqual(code, 0)
        self.assertIn("0.00 → 1.00", output)

    def test_history_and_rollback(self):
        run_cli(["evolve", "--provider", "mock", "--workdir", self.workdir, "--fresh"])
        code, output = run_cli(["history", "--provider", "mock", "--workdir", self.workdir])
        self.assertEqual(code, 0)
        self.assertIn("v1", output)
        code, output = run_cli(
            ["rollback", "--provider", "mock", "--workdir", self.workdir, "--to", "1"]
        )
        self.assertEqual(code, 0)
        self.assertIn("new head", output)


if __name__ == "__main__":
    unittest.main()
