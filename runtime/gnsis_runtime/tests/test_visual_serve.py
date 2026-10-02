from __future__ import annotations

import builtins

import pytest
from starlette.testclient import TestClient

from gnsis_runtime.visual.schema import Decision, Target
from gnsis_runtime.visual.serve import build_app, main


class FixedPolicy:
    name = "fixed"

    def decide(
        self,
        frame,
        goal,
        history,
        motion,
        viewport,
        cache,
        allowed_actions=None,
    ):
        return Decision(
            "click",
            0.9,
            Target(10, 10),
            frame_id=frame.frame_id,
        )


def test_build_app_serves_health_and_session_lifecycle() -> None:
    with TestClient(build_app(FixedPolicy(), "host-token")) as client:
        assert client.get("/health").json()["status"] == "ok"

        opened = client.post(
            "/v1/visual/sessions",
            headers={"Authorization": "Bearer host-token"},
        )
        assert opened.status_code == 200
        body = opened.json()
        planner_headers = {
            "Authorization": f"Bearer {body['planner']['token']}",
        }
        task = client.put(
            f"/v1/visual/sessions/{body['session_id']}/task",
            headers=planner_headers,
            json={"goal": "click the control", "allowed_actions": ["click"]},
        )
        assert task.status_code == 200
        assert (
            client.get(
                f"/v1/visual/sessions/{body['session_id']}",
                headers=planner_headers,
            ).status_code
            == 200
        )
        closed = client.delete(
            f"/v1/visual/sessions/{body['session_id']}",
            headers={"Authorization": "Bearer host-token"},
        )
        assert closed.json() == {"closed": True}


def test_main_requires_host_token_before_loading_model_stack(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.delenv("GNSIS_VISUAL_HOST_TOKEN", raising=False)
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name in {
            "torch",
            "gnsis_runtime.visual.backbone",
            "gnsis_runtime.visual.engine",
        }:
            raise AssertionError("model stack imported before host token validation")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    with pytest.raises(SystemExit) as error:
        main(["--model", "unused", "--head", "unused"])

    assert error.value.code == 2
    assert "GNSIS_VISUAL_HOST_TOKEN" in capsys.readouterr().err
