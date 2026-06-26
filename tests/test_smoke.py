"""Offline test for the gnsis-smoke harness (mock engine, local repo clone).

Exercises the smoke command's plumbing — clone, run an engine, report a patch —
without any network, key, or service deps. The live value (real model + repo) is
what you run by hand; this just guards the harness from breaking.
"""

import os
import subprocess
import tempfile
import unittest

from gnsis import smoke


def _make_repo(path: str) -> None:
    subprocess.run(["git", "-c", "init.defaultBranch=main", "init", path], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    with open(os.path.join(path, "README.md"), "w") as handle:
        handle.write("hello\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)


class SmokeTests(unittest.TestCase):
    def test_mock_engine_smoke_passes(self):
        src = tempfile.mkdtemp(prefix="gnsis-src-")
        _make_repo(src)
        rc = smoke.main(
            ["--repo", src, "-i", "add a marker", "--engine", "mock", "--base-branch", "main"]
        )
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
