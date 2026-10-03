from __future__ import annotations

import io
import time

import jwt
from PIL import Image
from starlette.testclient import TestClient
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from gnsis_runtime.screen import ScreenFrame
from gnsis_runtime.visual.api import (
    MAX_FRAME_HEADER_BYTES,
    VisualAPISettings,
    create_visual_api,
)
from gnsis_runtime.visual.grants import GrantVerifier
from gnsis_runtime.visual.perception import PerceivedElement, VisualPerception
from gnsis_runtime.visual.schema import Decision, Target
from gnsis_runtime.visual.service import VisualService

AUTH = {"Authorization": "Bearer test-token"}


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

    def perceive(self, frames, motion, viewport):
        return VisualPerception(
            summary="A browser page is visible.",
            visible_text=("Example",),
            elements=(
                PerceivedElement(
                    "Example heading",
                    "text",
                    "Example",
                    (0, 0, 64, 20),
                    "",
                    0.9,
                ),
            ),
            changes=(),
            confidence=0.9,
            frame_id=str(frames[-1].frame_id),
            observed_frame_ids=tuple(str(frame.frame_id) for frame in frames),
            motion=motion,
            viewport=viewport,
        )


def _jpeg() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 32), "white").save(buffer, format="JPEG")
    return buffer.getvalue()


def _app():
    service = VisualService(FixedPolicy())
    return create_visual_api(
        service,
        VisualAPISettings(host_token="test-token"),
    )


def test_api_settings_redacts_host_token_from_repr() -> None:
    assert "host-secret" not in repr(VisualAPISettings(host_token="host-secret"))


def _open(client: TestClient) -> dict:
    response = client.post("/v1/visual/sessions", headers=AUTH)
    assert response.status_code == 200
    return response.json()


def test_api_requires_authentication_and_unknown_sessions_are_structured() -> None:
    with TestClient(_app()) as client:
        assert client.post("/v1/visual/sessions").status_code == 401
        response = client.get("/v1/visual/sessions/missing", headers=AUTH)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "unknown_session"


def test_api_stream_task_decision_attempt_and_reset_contract() -> None:
    service = VisualService(FixedPolicy())
    app = create_visual_api(
        service,
        VisualAPISettings(host_token="test-token"),
    )
    with TestClient(app) as client:
        opened = _open(client)
        session_id = opened["session_id"]
        stream = opened["stream"]
        with client.websocket_connect(
            f"{stream['path']}?token={stream['token']}"
        ) as websocket:
            websocket.send_json(
                {
                    "type": "screen.frame",
                    "frame_id": "f1",
                    "captured_at_ms": 1000,
                    "encoding": "jpeg",
                    "video_source": "screen",
                }
            )
            frame_body = _jpeg()
            websocket.send_bytes(frame_body)
            accepted = websocket.receive_json()
        assert accepted["type"] == "screen.frame.accepted"
        assert accepted["frame_id"] == "f1"
        assert accepted["frame_seq"] == 1
        assert service.state(session_id)["usage"]["frame_bytes"] == len(frame_body)

        perception = client.post(
            f"/v1/visual/sessions/{session_id}/perceptions",
            headers=AUTH,
            json={"request_id": "perception-1"},
        )
        assert perception.status_code == 200
        assert perception.json()["perception"]["summary"] == (
            "A browser page is visible."
        )
        assert perception.json()["perception"]["frame_id"] == "f1"

        task = client.put(
            f"/v1/visual/sessions/{session_id}/task",
            headers=AUTH,
            json={
                "goal": "click the control",
                "allowed_actions": ["click", "wait"],
            },
        )
        assert task.status_code == 200
        decision = client.post(
            f"/v1/visual/sessions/{session_id}/decisions",
            headers=AUTH,
            json={"request_id": "request-1"},
        )
        assert decision.status_code == 200
        assert decision.json()["decision"]["frame_id"] == "f1"
        recorded = client.post(
            f"/v1/visual/sessions/{session_id}/attempts",
            headers=AUTH,
            json={"decision_id": decision.json()["decision_id"]},
        )
        assert recorded.status_code == 200
        assert recorded.json()["recorded"] is True
        reset = client.delete(
            f"/v1/visual/sessions/{session_id}/task",
            headers=AUTH,
        )
        assert reset.status_code == 200
        assert reset.json()["goal"] is None


def test_api_scopes_planner_credentials_to_task_and_read_operations() -> None:
    with TestClient(_app()) as client:
        opened = _open(client)
        second = _open(client)
        session_id = opened["session_id"]
        planner_auth = {
            "Authorization": f"Bearer {opened['planner']['token']}",
        }

        with client.websocket_connect(
            f"{opened['stream']['path']}?token={opened['stream']['token']}"
        ) as websocket:
            websocket.send_json(
                {
                    "type": "screen.frame",
                    "frame_id": "planner-frame",
                    "captured_at_ms": 1000,
                    "encoding": "jpeg",
                }
            )
            websocket.send_bytes(_jpeg())
            assert websocket.receive_json()["type"] == "screen.frame.accepted"

        perception = client.post(
            f"/v1/visual/sessions/{session_id}/perceptions",
            headers=planner_auth,
            json={"request_id": "planner-perception"},
        )
        assert perception.status_code == 200
        assert perception.json()["perception"]["frame_id"] == "planner-frame"

        task = client.put(
            f"/v1/visual/sessions/{session_id}/task",
            headers=planner_auth,
            json={"goal": "click the control", "allowed_actions": ["click"]},
        )
        assert task.status_code == 200
        decision = client.post(
            f"/v1/visual/sessions/{session_id}/decisions",
            headers=planner_auth,
            json={"request_id": "planner-request"},
        )
        assert decision.status_code == 200
        assert decision.json()["frame_seq"] == 1
        assert (
            client.get(
                f"/v1/visual/sessions/{session_id}",
                headers=planner_auth,
            ).status_code
            == 200
        )

        planner_attempt = client.post(
            f"/v1/visual/sessions/{session_id}/attempts",
            headers=planner_auth,
            json={"decision_id": decision.json()["decision_id"]},
        )
        assert planner_attempt.status_code == 403
        assert planner_attempt.json()["error"]["code"] == "forbidden"

        planner_create = client.post(
            "/v1/visual/sessions",
            headers=planner_auth,
        )
        assert planner_create.status_code == 403
        planner_close = client.delete(
            f"/v1/visual/sessions/{session_id}",
            headers=planner_auth,
        )
        assert planner_close.status_code == 403
        other_session = client.get(
            f"/v1/visual/sessions/{second['session_id']}",
            headers=planner_auth,
        )
        assert other_session.status_code == 403
        assert other_session.json()["error"]["code"] == "forbidden"
        other_perception = client.post(
            f"/v1/visual/sessions/{second['session_id']}/perceptions",
            headers=planner_auth,
            json={"request_id": "other-perception"},
        )
        assert other_perception.status_code == 403
        assert other_perception.json()["error"]["code"] == "forbidden"

        unknown_session = client.get(
            "/v1/visual/sessions/missing",
            headers=planner_auth,
        )
        assert unknown_session.status_code == 401
        invalid_token = client.get(
            f"/v1/visual/sessions/{session_id}",
            headers={"Authorization": "Bearer invalid"},
        )
        assert invalid_token.status_code == 401

        host_attempt = client.post(
            f"/v1/visual/sessions/{session_id}/attempts",
            headers=AUTH,
            json={"decision_id": decision.json()["decision_id"]},
        )
        assert host_attempt.status_code == 200
        planner_reset = client.delete(
            f"/v1/visual/sessions/{session_id}/task",
            headers=planner_auth,
        )
        assert planner_reset.status_code == 200
        assert (
            client.delete(
                f"/v1/visual/sessions/{session_id}",
                headers=AUTH,
            ).status_code
            == 200
        )


def test_api_rejects_missing_task_and_stale_stream_frames() -> None:
    with TestClient(_app()) as client:
        opened = _open(client)
        session_id = opened["session_id"]
        stream = opened["stream"]
        missing_task = client.post(
            f"/v1/visual/sessions/{session_id}/decisions",
            headers=AUTH,
            json={"request_id": "request-1"},
        )
        assert missing_task.status_code == 409
        assert missing_task.json()["error"]["code"] == "task_required"

        with client.websocket_connect(
            f"{stream['path']}?token={stream['token']}"
        ) as websocket:
            for frame_id, captured_at_ms in (("f1", 1000), ("f2", 900)):
                websocket.send_json(
                    {
                        "type": "screen.frame",
                        "frame_id": frame_id,
                        "captured_at_ms": captured_at_ms,
                        "encoding": "jpeg",
                    }
                )
                websocket.send_bytes(_jpeg())
                response = websocket.receive_json()
                if frame_id == "f1":
                    assert response["type"] == "screen.frame.accepted"
                else:
                    assert response["type"] == "screen.frame.rejected"
                    assert response["error"]["code"] == "stale_frame"


def test_api_discards_binary_after_rejected_headers_and_resynchronizes() -> None:
    invalid_headers = [
        "x" * (MAX_FRAME_HEADER_BYTES + 1),
        "{",
        "[]",
        "{}",
    ]
    with TestClient(_app()) as client:
        opened = _open(client)
        with client.websocket_connect(
            f"{opened['stream']['path']}?token={opened['stream']['token']}"
        ) as websocket:
            for index, invalid_header in enumerate(invalid_headers):
                websocket.send_text(invalid_header)
                rejected = websocket.receive_json()
                assert rejected["type"] == "screen.frame.rejected"
                assert rejected["error"]["code"] == "invalid_protocol"

                websocket.send_bytes(_jpeg())
                frame_id = f"resynchronized-{index}"
                websocket.send_json(
                    {
                        "type": "screen.frame",
                        "frame_id": frame_id,
                        "captured_at_ms": 1_000 + index,
                        "encoding": "jpeg",
                        "video_source": "screen",
                    }
                )
                websocket.send_bytes(_jpeg())
                accepted = websocket.receive_json()
                assert accepted["type"] == "screen.frame.accepted"
                assert accepted["frame_id"] == frame_id


def test_api_has_no_screenshot_upload_decision_path() -> None:
    with TestClient(_app()) as client:
        response = client.post(
            "/v1/visual/decide",
            headers=AUTH,
            files={"image": ("screen.jpg", _jpeg(), "image/jpeg")},
        )

    assert response.status_code == 404


def _grant(
    private_pem: str,
    *,
    workspace_id: str,
    grant_id: str,
    key_id: str | None = None,
    project_id: str | None = "project",
    environment_id: str | None = "environment",
    expires_in: int = 300,
    max_concurrent_sessions: int = 4,
) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": "control-plane",
            "aud": "gnsis-visual",
            "sub": key_id or f"key-{workspace_id}",
            "jti": grant_id,
            "iat": now - 600 if expires_in < 0 else now,
            "exp": now + expires_in,
            "ws": workspace_id,
            "prj": project_id,
            "env": environment_id,
            "scp": ["visual:host"],
            "lim": {
                "max_concurrent_sessions": max_concurrent_sessions,
                "max_decisions_per_session": 2,
                "max_frames_per_session": 10,
            },
        },
        private_pem,
        algorithm="EdDSA",
    )


def test_api_accepts_grants_and_isolates_tenants_on_session_routes() -> None:
    private = Ed25519PrivateKey.generate()
    private_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    service = VisualService(FixedPolicy())
    app = create_visual_api(
        service,
        VisualAPISettings(
            host_token="operator-token",
            grant_verifier=GrantVerifier(public_pem, issuer="control-plane"),
        ),
    )
    grant_a = _grant(private_pem, workspace_id="workspace-a", grant_id="grant-a")
    grant_b = _grant(private_pem, workspace_id="workspace-b", grant_id="grant-b")

    with TestClient(app) as client:
        opened = client.post(
            "/v1/visual/sessions",
            headers={"Authorization": f"Bearer {grant_a}"},
        )
        assert opened.status_code == 200
        session_id = opened.json()["session_id"]
        tenant_b = {"Authorization": f"Bearer {grant_b}"}
        close = client.delete(
            f"/v1/visual/sessions/{session_id}",
            headers=tenant_b,
        )
        attempt = client.post(
            f"/v1/visual/sessions/{session_id}/attempts",
            headers=tenant_b,
            json={"decision_id": "missing"},
        )
        assert close.status_code == 404
        assert close.json()["error"]["code"] == "unknown_session"
        assert attempt.status_code == 404
        assert attempt.json()["error"]["code"] == "unknown_session"
        assert (
            client.get(
                f"/v1/visual/sessions/{session_id}",
                headers={"Authorization": "Bearer operator-token"},
            ).status_code
            == 200
        )


def test_api_enforces_grant_concurrent_session_limit() -> None:
    private = Ed25519PrivateKey.generate()
    private_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    app = create_visual_api(
        VisualService(FixedPolicy()),
        VisualAPISettings(
            grant_verifier=GrantVerifier(public_pem, issuer="control-plane")
        ),
    )
    grant = _grant(
        private_pem,
        workspace_id="workspace-a",
        grant_id="grant-a",
        max_concurrent_sessions=1,
    )
    headers = {"Authorization": f"Bearer {grant}"}

    with TestClient(app) as client:
        assert client.post("/v1/visual/sessions", headers=headers).status_code == 200
        second = client.post("/v1/visual/sessions", headers=headers)

    assert second.status_code == 429
    assert second.json()["error"]["code"] == "quota_exceeded"


def test_expired_grant_closes_session_but_cannot_create_one() -> None:
    private = Ed25519PrivateKey.generate()
    private_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    app = create_visual_api(
        VisualService(FixedPolicy()),
        VisualAPISettings(
            grant_verifier=GrantVerifier(public_pem, issuer="control-plane")
        ),
    )
    live = _grant(private_pem, workspace_id="workspace-a", grant_id="grant-live")
    expired = _grant(
        private_pem,
        workspace_id="workspace-a",
        grant_id="grant-old",
        expires_in=-60,
    )

    with TestClient(app) as client:
        opened = client.post(
            "/v1/visual/sessions", headers={"Authorization": f"Bearer {live}"}
        )
        assert opened.status_code == 200
        session_id = opened.json()["session_id"]
        closed = client.delete(
            f"/v1/visual/sessions/{session_id}",
            headers={"Authorization": f"Bearer {expired}"},
        )
        create = client.post(
            "/v1/visual/sessions", headers={"Authorization": f"Bearer {expired}"}
        )

    assert closed.status_code == 200
    assert create.status_code == 401


def test_expired_grant_cannot_record_attempt() -> None:
    private = Ed25519PrivateKey.generate()
    private_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    service = VisualService(FixedPolicy())
    app = create_visual_api(
        service,
        VisualAPISettings(
            grant_verifier=GrantVerifier(public_pem, issuer="control-plane")
        ),
    )
    live = _grant(private_pem, workspace_id="workspace-a", grant_id="grant-live")
    expired = _grant(
        private_pem,
        workspace_id="workspace-a",
        grant_id="grant-old",
        expires_in=-60,
    )

    with TestClient(app) as client:
        opened = client.post(
            "/v1/visual/sessions", headers={"Authorization": f"Bearer {live}"}
        ).json()
        session_id = opened["session_id"]
        service.set_task(session_id, "click")
        service.publish_frame(
            session_id,
            ScreenFrame(
                image=Image.new("RGB", (64, 32), "white"),
                frame_id="f1",
                captured_at_ms=1000,
            ),
        )
        decision_id = service.decide(session_id, "request-1")["decision_id"]
        attempted = client.post(
            f"/v1/visual/sessions/{session_id}/attempts",
            headers={"Authorization": f"Bearer {expired}"},
            json={"decision_id": decision_id},
        )

    assert attempted.status_code == 401


def test_same_workspace_grants_from_other_keys_are_isolated() -> None:
    private = Ed25519PrivateKey.generate()
    private_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    app = create_visual_api(
        VisualService(FixedPolicy()),
        VisualAPISettings(
            grant_verifier=GrantVerifier(public_pem, issuer="control-plane")
        ),
    )
    grant_a = _grant(private_pem, workspace_id="w", grant_id="g-a", key_id="key-a")
    grant_b = _grant(private_pem, workspace_id="w", grant_id="g-b", key_id="key-b")
    grant_wrong_project = _grant(
        private_pem,
        workspace_id="w",
        grant_id="g-p",
        key_id="key-a",
        project_id="other-project",
    )

    with TestClient(app) as client:
        opened = client.post(
            "/v1/visual/sessions", headers={"Authorization": f"Bearer {grant_a}"}
        )
        session_id = opened.json()["session_id"]
        other_key = client.delete(
            f"/v1/visual/sessions/{session_id}",
            headers={"Authorization": f"Bearer {grant_b}"},
        )
        other_project = client.delete(
            f"/v1/visual/sessions/{session_id}",
            headers={"Authorization": f"Bearer {grant_wrong_project}"},
        )

    assert other_key.status_code == 404
    assert other_key.json()["error"]["code"] == "unknown_session"
    assert other_project.status_code == 404


def test_unscoped_grant_cannot_enter_scoped_session() -> None:
    private = Ed25519PrivateKey.generate()
    private_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    app = create_visual_api(
        VisualService(FixedPolicy()),
        VisualAPISettings(
            grant_verifier=GrantVerifier(public_pem, issuer="control-plane")
        ),
    )
    scoped = _grant(
        private_pem,
        workspace_id="workspace-a",
        grant_id="grant-scoped",
    )
    unscoped = _grant(
        private_pem,
        workspace_id="workspace-a",
        grant_id="grant-unscoped",
        project_id=None,
        environment_id=None,
    )

    with TestClient(app) as client:
        opened = client.post(
            "/v1/visual/sessions",
            headers={"Authorization": f"Bearer {scoped}"},
        ).json()
        response = client.get(
            f"/v1/visual/sessions/{opened['session_id']}",
            headers={"Authorization": f"Bearer {unscoped}"},
        )

    assert response.status_code == 404


def test_health_exposes_usage_sink_state() -> None:
    class Sink:
        def health(self) -> dict:
            return {
                "pending": 2,
                "dropped": 1,
                "failures": 3,
                "last_error": "OSError",
                "running": True,
            }

    service = VisualService(FixedPolicy())
    app = create_visual_api(service, VisualAPISettings(host_token="operator"))

    with TestClient(app) as client:
        assert client.get("/health").json()["metering"] == "ok"
        app.state.usage_sink = Sink()
        report = client.get("/health").json()

    assert report["metering"] == "degraded"
    assert report["usage_sink"]["failures"] == 3
