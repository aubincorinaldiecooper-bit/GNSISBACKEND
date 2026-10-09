from __future__ import annotations

import unittest

from scripts.coolify_simplemem_setup import SetupError, choose_unique, get_identity, repo_name


class CoolifySimpleMemSetupTests(unittest.TestCase):
    def test_repo_name_normalizes_github_remote_forms(self) -> None:
        self.assertEqual(
            repo_name("https://github.com/AubinCorinaldieCooper/GNSISBACKEND.git"),
            "aubincorinaldiecooper/gnsisbackend",
        )
        self.assertEqual(
            repo_name("git@github.com:AubinCorinaldieCooper/GNSISBACKEND.git"),
            "aubincorinaldiecooper/gnsisbackend",
        )

    def test_get_identity_reads_the_matching_nested_resource(self) -> None:
        detail = {
            "project": {"uuid": "project-uuid"},
            "server": {"uuid": "server-uuid"},
        }
        self.assertEqual(get_identity(detail, "project_uuid", "project"), "project-uuid")
        self.assertEqual(get_identity(detail, "server_uuid", "server"), "server-uuid")

    def test_choose_unique_refuses_ambiguous_resources(self) -> None:
        with self.assertRaisesRegex(SetupError, "found 2"):
            choose_unique([{"uuid": "one"}, {"uuid": "two"}], "worker")


if __name__ == "__main__":
    unittest.main()
