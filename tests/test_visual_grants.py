"""Scoped visual grants and usage-callback contract tests."""

from __future__ import annotations

import os
import sys
import time
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import jwt  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from _authkit import AUDIENCE, ISSUER, fresh_sqlite_env, make_keypair, mint  # noqa: E402

VISUAL_ISSUER = "gnsis-control-plane"
USAGE_SECRET = "visual-usage-secret"


class VisualGrantTests(unittest.TestCase):
    def setUp(self):
        fresh_sqlite_env()
        os.environ["GNSIS_VIRTUAL_KEY_PEPPER"] = "visual-test-pepper"
        os.environ["GNSIS_VISUAL_GRANT_ISSUER"] = VISUAL_ISSUER
        os.environ["GNSIS_VISUAL_GRANT_TTL_S"] = "120"
        os.environ["GNSIS_VISUAL_USAGE_SECRET"] = USAGE_SECRET
        private_key = Ed25519PrivateKey.generate()
        self.private_pem = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        self.public_pem = (
            private_key.public_key()
            .public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode()
        )
        os.environ["GNSIS_VISUAL_GRANT_PRIVATE_KEY"] = self.private_pem

        from gnsis.service import settings as settings_mod

        settings_mod._settings = None
        from gnsis.service.db import init_db

        init_db()
        from fastapi.testclient import TestClient

        from gnsis.service import api
        from gnsis.service.auth import JwksCache, JwtVerifier

        self.api = api
        self.client = TestClient(api.app)
        self.workspace_id = "ws-visual-test"
        api.app.dependency_overrides[api.current_workspace] = lambda: (
            types.SimpleNamespace(id=self.workspace_id)
        )
        session_private, jwks = make_keypair("visual-session-key")
        verifier = JwtVerifier(
            JwksCache(fetcher=lambda: jwks), issuer=ISSUER, audience=AUDIENCE
        )
        api.app.dependency_overrides[api.get_verifier] = lambda: verifier
        self.dashboard_token = mint(
            session_private, "visual-session-key", "visual-dashboard-user"
        )
        self.settings = settings_mod.get_settings()

        from gnsis.service.virtual_keys import VirtualKeyStore

        self.keys = VirtualKeyStore()

    def tearDown(self):
        self.api.app.dependency_overrides.clear()

    def _create_key(self, scopes=None, *, workspace_id=None, project_id=None):
        return self.keys.create(
            self.settings,
            workspace_id=workspace_id or self.workspace_id,
            name="visual test",
            project_id=project_id,
            environment_id="env-test",
            api_scopes=scopes,
        )

    @staticmethod
    def _auth(secret):
        return {"Authorization": f"Bearer {secret}"}

    def _grant(self, secret):
        return self.client.post("/v1/visual/grants", headers=self._auth(secret))

    def _usage_report(
        self, *, session_id="session-1", report_seq=1, key_id, **overrides
    ):
        report = {
            "event_id": f"{session_id}:{report_seq}",
            "workspace_id": self.workspace_id,
            "virtual_key_id": key_id,
            "project_id": None,
            "environment_id": "env-test",
            "grant_id": "grant-test",
            "session_id": session_id,
            "report_seq": report_seq,
            "frames_accepted": 1,
            "frame_bytes": 1024,
            "decisions": 1,
            "decisions_act": 1,
            "decisions_abstain": 0,
            "perceptions": 0,
            "attempts_recorded": 1,
            "inference_ms": 25,
            "session_ms": 1000,
            "closed": False,
            "generated_at_ms": int(time.time() * 1000),
        }
        report.update(overrides)
        return report

    def _post_usage(self, reports, secret=USAGE_SECRET):
        headers = self._auth(secret) if secret else {}
        return self.client.post(
            "/internal/usage/visual", json={"reports": reports}, headers=headers
        )

    def test_visual_key_receives_verifiable_limited_grant(self):
        view, key_secret = self._create_key(
            ["visual:host"], project_id="project-visual"
        )

        response = self._grant(key_secret)

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        claims = jwt.decode(
            body["grant"],
            self.public_pem,
            algorithms=["EdDSA"],
            issuer=VISUAL_ISSUER,
            audience="gnsis-visual",
        )
        self.assertEqual(claims["iss"], VISUAL_ISSUER)
        self.assertEqual(claims["aud"], "gnsis-visual")
        self.assertEqual(claims["sub"], view.id)
        self.assertEqual(claims["ws"], self.workspace_id)
        self.assertEqual(claims["prj"], "project-visual")
        self.assertEqual(claims["env"], "env-test")
        self.assertEqual(claims["scp"], ["visual:host"])
        self.assertEqual(claims["exp"] - claims["iat"], 120)
        self.assertEqual(
            claims["lim"],
            {
                "max_concurrent_sessions": 4,
                "max_decisions_per_session": 2000,
                "max_frames_per_session": 100_000,
            },
        )
        self.assertEqual(body["limits"], claims["lim"])
        self.assertTrue(body["expires_at"].endswith("+00:00"))

    def test_legacy_narrow_and_dashboard_credentials_cannot_mint_grants(self):
        _, legacy_secret = self._create_key()
        _, narrow_secret = self._create_key(["runs:read"])

        self.assertEqual(self._grant(legacy_secret).status_code, 403)
        self.assertEqual(self._grant(narrow_secret).status_code, 403)
        dashboard = self._grant(self.dashboard_token)
        self.assertEqual(dashboard.status_code, 403, dashboard.text)
        self.assertEqual(dashboard.json()["error"]["code"], "authorization_failed")

    def test_bad_key_and_unconfigured_signing_are_rejected(self):
        bad_key = self._grant("gns_live_not-a-real-key")
        self.assertEqual(bad_key.status_code, 401)
        self.assertEqual(bad_key.json()["error"]["code"], "authentication_failed")

        _, key_secret = self._create_key(["visual:host"])
        from gnsis.service import settings as settings_mod

        os.environ["GNSIS_VISUAL_GRANT_ISSUER"] = ""
        settings_mod._settings = None
        unavailable_issuer = self._grant(key_secret)
        self.assertEqual(unavailable_issuer.status_code, 503, unavailable_issuer.text)
        self.assertEqual(
            unavailable_issuer.json()["error"]["code"], "visual_grants_unavailable"
        )

        os.environ["GNSIS_VISUAL_GRANT_ISSUER"] = VISUAL_ISSUER
        os.environ.pop("GNSIS_VISUAL_GRANT_PRIVATE_KEY")
        settings_mod._settings = None
        unavailable_key = self._grant(key_secret)
        self.assertEqual(unavailable_key.status_code, 503, unavailable_key.text)

    def test_daily_quota_is_per_virtual_key(self):
        first_view, first_secret = self._create_key(["visual:host"])
        _, second_secret = self._create_key(["visual:host"])
        os.environ["GNSIS_VISUAL_DAILY_DECISION_QUOTA"] = "3"
        from gnsis.service import settings as settings_mod

        settings_mod._settings = None
        self.settings = settings_mod.get_settings()
        report = self._usage_report(
            key_id=first_view.id,
            decisions=3,
            decisions_act=2,
            decisions_abstain=1,
        )

        ingested = self._post_usage([report])
        blocked = self._grant(first_secret)
        allowed = self._grant(second_secret)

        self.assertEqual(ingested.status_code, 200, ingested.text)
        self.assertEqual(blocked.status_code, 429, blocked.text)
        self.assertEqual(blocked.json()["error"]["code"], "quota_exceeded")
        self.assertEqual(allowed.status_code, 200, allowed.text)

    def test_daily_quota_counts_panoptic_perceptions(self):
        view, secret = self._create_key(["visual:host"])
        os.environ["GNSIS_VISUAL_DAILY_DECISION_QUOTA"] = "3"
        from gnsis.service import settings as settings_mod

        settings_mod._settings = None
        self.settings = settings_mod.get_settings()
        report = self._usage_report(
            key_id=view.id,
            decisions=1,
            decisions_act=1,
            perceptions=2,
        )

        ingested = self._post_usage([report])
        blocked = self._grant(secret)

        self.assertEqual(ingested.status_code, 200, ingested.text)
        self.assertEqual(blocked.status_code, 429, blocked.text)
        self.assertEqual(blocked.json()["error"]["code"], "quota_exceeded")

    def test_delayed_usage_attributes_to_report_day_not_ingest_day(self):
        view, secret = self._create_key(["visual:host"])
        os.environ["GNSIS_VISUAL_DAILY_DECISION_QUOTA"] = "3"
        from gnsis.service import settings as settings_mod

        settings_mod._settings = None
        self.settings = settings_mod.get_settings()
        yesterday_ms = int(time.time() * 1000) - 36 * 3600 * 1000
        report = self._usage_report(
            key_id=view.id,
            decisions=3,
            generated_at_ms=yesterday_ms,
        )

        ingested = self._post_usage([report])
        granted = self._grant(secret)

        self.assertEqual(ingested.status_code, 200, ingested.text)
        self.assertEqual(granted.status_code, 200, granted.text)

    def test_legacy_usage_without_generated_timestamp_uses_ingestion_time(self):
        view, _ = self._create_key(["visual:host"])
        report = self._usage_report(key_id=view.id)
        report.pop("generated_at_ms")
        report.pop("perceptions")

        ingested = self._post_usage([report])

        self.assertEqual(ingested.status_code, 200, ingested.text)
        from gnsis.service import orm
        from gnsis.service.db import session_scope

        with session_scope() as session:
            row = session.query(orm.VisualUsageRecord).one()
        self.assertIsNotNone(row.reported_at)
        self.assertEqual(row.perceptions, 0)

    def test_usage_callback_authentication_idempotency_and_validation(self):
        view, _ = self._create_key(["visual:host"])
        report = self._usage_report(key_id=view.id)
        os.environ.pop("GNSIS_VISUAL_USAGE_SECRET")
        from gnsis.service import settings as settings_mod

        settings_mod._settings = None
        missing_secret = self._post_usage([report], secret=None)
        os.environ["GNSIS_VISUAL_USAGE_SECRET"] = USAGE_SECRET
        settings_mod._settings = None
        wrong_secret = self._post_usage([report], secret="wrong")
        self.assertEqual(missing_secret.status_code, 503)
        self.assertEqual(wrong_secret.status_code, 401)

        first = self._post_usage([report])
        duplicate = self._post_usage([report])
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json(), {"accepted": 1, "duplicates": 0})
        self.assertEqual(duplicate.status_code, 200, duplicate.text)
        self.assertEqual(duplicate.json(), {"accepted": 0, "duplicates": 1})

        negative = self._post_usage(
            [self._usage_report(key_id=view.id, frame_bytes=-1, report_seq=2)]
        )
        too_many = self._post_usage(
            [
                self._usage_report(
                    key_id=view.id,
                    session_id=f"session-{index}",
                    report_seq=1,
                )
                for index in range(501)
            ]
        )
        self.assertEqual(negative.status_code, 400, negative.text)
        self.assertIn(too_many.status_code, (400, 413), too_many.text)

        oversized_id = self._post_usage(
            [self._usage_report(key_id=view.id, report_seq=3, event_id="x" * 200)]
        )
        self.assertEqual(oversized_id.status_code, 400, oversized_id.text)
        oversized_counter = self._post_usage(
            [self._usage_report(key_id=view.id, report_seq=4, decisions=2**63)]
        )
        self.assertEqual(oversized_counter.status_code, 400, oversized_counter.text)
        misattributed = self._post_usage(
            [
                self._usage_report(
                    key_id=view.id,
                    report_seq=5,
                    project_id="not-the-keys-project",
                )
            ]
        )
        self.assertEqual(misattributed.status_code, 400, misattributed.text)
        unknown_key = self._post_usage(
            [self._usage_report(key_id="vkey-does-not-exist", report_seq=6)]
        )
        self.assertEqual(unknown_key.status_code, 400, unknown_key.text)
        stale_timestamp = self._post_usage(
            [
                self._usage_report(
                    key_id=view.id,
                    report_seq=7,
                    generated_at_ms=int(time.time() * 1000) + 10 * 60 * 1000,
                )
            ]
        )
        self.assertEqual(stale_timestamp.status_code, 400, stale_timestamp.text)

        previous_limit = os.environ.get("GNSIS_EXECUTOR_CALLBACK_MAX_BYTES")
        try:
            os.environ["GNSIS_EXECUTOR_CALLBACK_MAX_BYTES"] = "10"
            settings_mod._settings = None
            too_large = self._post_usage([])
            self.assertEqual(too_large.status_code, 413, too_large.text)
        finally:
            if previous_limit is None:
                os.environ.pop("GNSIS_EXECUTOR_CALLBACK_MAX_BYTES", None)
            else:
                os.environ["GNSIS_EXECUTOR_CALLBACK_MAX_BYTES"] = previous_limit
            settings_mod._settings = None

        from gnsis.service import orm
        from gnsis.service.db import session_scope

        with session_scope() as session:
            rows = session.query(orm.VisualUsageRecord).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].event_id, "session-1:1")

    def test_usage_read_api_sums_counters_by_day_key_and_client(self):
        from gnsis.service import workspaces as ws

        # The dashboard session resolves to its subject's workspace.
        self.workspace_id = ws.get_or_create_workspace("visual-dashboard-user").id
        view, secret = self._create_key(["visual:host"])
        other, other_secret = self._create_key(["visual:host"])
        public_only, public_secret = self._create_key()
        reports = [
            self._usage_report(
                key_id=view.id,
                report_seq=1,
                session_ms=10_000,
                inspections=2,
                pixel_reads=1,
                history_reads=3,
                host_client="gnsis-visual-browser-host/0.1",
                planner_client="claude-code/2.1",
            ),
            self._usage_report(
                key_id=view.id,
                report_seq=2,
                session_ms=5_000,
                closed=True,
                host_client="gnsis-visual-browser-host/0.1",
                planner_client="claude-code/2.1",
            ),
            self._usage_report(
                key_id=other.id,
                session_id="session-2",
                session_ms=7_000,
                host_client="codex/0.4",
            ),
        ]
        legacy = self._usage_report(key_id=other.id, session_id="session-3")
        self.assertEqual(self._post_usage(reports + [legacy]).status_code, 200)

        workspace = self.client.get(
            "/v1/visual/usage", headers=self._auth(self.dashboard_token)
        )
        self.assertEqual(workspace.status_code, 200, workspace.text)
        body = workspace.json()
        self.assertEqual(body["totals"]["session_ms"], 23_000)
        self.assertEqual(body["totals"]["inspections"], 2)
        self.assertEqual(body["totals"]["pixel_reads"], 1)
        self.assertEqual(body["totals"]["history_reads"], 3)
        self.assertEqual(body["totals"]["sessions"], 3)
        self.assertEqual(len(body["by_day"]), 1)
        clients = {row["client"]: row["session_ms"] for row in body["by_client"]}
        self.assertEqual(
            clients, {"claude-code/2.1": 15_000, "codex/0.4": 7_000, "unknown": 1000}
        )

        own = self.client.get("/v1/visual/usage", headers=self._auth(secret))
        self.assertEqual(own.status_code, 200, own.text)
        self.assertEqual(own.json()["totals"]["session_ms"], 15_000)
        self.assertEqual(
            [row["virtual_key_id"] for row in own.json()["by_key"]], [view.id]
        )
        foreign = self.client.get(
            f"/v1/visual/usage?virtual_key_id={other.id}", headers=self._auth(secret)
        )
        self.assertEqual(foreign.status_code, 403, foreign.text)
        unscoped = self.client.get(
            "/v1/visual/usage", headers=self._auth(public_secret)
        )
        self.assertEqual(unscoped.status_code, 403, unscoped.text)
        bad_days = self.client.get(
            "/v1/visual/usage?days=0", headers=self._auth(self.dashboard_token)
        )
        self.assertEqual(bad_days.status_code, 400, bad_days.text)
        oversized_client = self._post_usage(
            [
                self._usage_report(
                    key_id=view.id, session_id="session-4", planner_client="x" * 129
                )
            ]
        )
        self.assertEqual(oversized_client.status_code, 400, oversized_client.text)

    def test_visual_key_api_round_trips_scopes_rejects_unknown_and_rotates_them(self):
        created = self.client.post(
            "/v1/virtual-keys",
            json={"name": "visual", "api_scopes": ["visual:host"]},
        )
        self.assertEqual(created.status_code, 200, created.text)
        key = created.json()["virtual_key"]
        self.assertEqual(key["api_scopes"], ["visual:host"])

        unknown = self.client.post(
            "/v1/virtual-keys",
            json={"api_scopes": ["visual:unknown"]},
        )
        self.assertEqual(unknown.status_code, 400, unknown.text)
        self.assertIn("unknown scope: visual:unknown", unknown.text)

        rotated = self.client.post(f"/v1/virtual-keys/{key['id']}/rotate")
        self.assertEqual(rotated.status_code, 200, rotated.text)
        self.assertEqual(rotated.json()["virtual_key"]["api_scopes"], ["visual:host"])


if __name__ == "__main__":
    unittest.main()
