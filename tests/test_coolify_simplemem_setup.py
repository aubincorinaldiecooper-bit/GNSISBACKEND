from __future__ import annotations

import pytest

from scripts.coolify_simplemem_setup import SetupError, choose_unique, get_identity, repo_name


def test_repo_name_normalizes_github_remote_forms() -> None:
    assert repo_name("https://github.com/AubinCorinaldieCooper/GNSISBACKEND.git") == (
        "aubincorinaldiecooper/gnsisbackend"
    )
    assert repo_name("git@github.com:AubinCorinaldieCooper/GNSISBACKEND.git") == (
        "aubincorinaldiecooper/gnsisbackend"
    )


def test_get_identity_reads_the_matching_nested_resource() -> None:
    detail = {
        "project": {"uuid": "project-uuid"},
        "server": {"uuid": "server-uuid"},
    }
    assert get_identity(detail, "project_uuid", "project") == "project-uuid"
    assert get_identity(detail, "server_uuid", "server") == "server-uuid"


def test_choose_unique_refuses_ambiguous_resources() -> None:
    with pytest.raises(SetupError, match="found 2"):
        choose_unique([{"uuid": "one"}, {"uuid": "two"}], "worker")
