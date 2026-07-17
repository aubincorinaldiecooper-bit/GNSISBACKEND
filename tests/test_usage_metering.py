"""PR 1 — LiteLLM metering + trace correlation.

Covers the required automated surface: callback authentication, deterministic
attribution, duplicate-callback idempotency, token/cost persistence (exact
decimal), failed-request persistence, retry relationship, cross-workspace
isolation, native-run linkage, and external virtual-key linkage.
"""

from __future__ import annotations

import os
import sys
import types
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from _authkit import fresh_sqlite_env  # noqa: E402

SECRET = "cb-secret"
PATH = "/internal/usage/litellm/callback"


def _configure():
    fresh_sqlite_env()
    os.environ["GNSIS_LITELLM_CALLBACK_SECRET"] = SECRET
    from gnsis.service import settings as settings_mod

    settings_mod._settings = None
    from gnsis.service.db import init_db

    init_db()


def _body(rid="req-1", workspace="ws-1", user="user-1", metadata=None, **top):
    md = {"workspace_id": workspace, "user_id": user}
    md.update(metadata or {})
    body = {
        "litellm_request_id": rid,
        "provider": "anthropic",
        "model": "anthropic/claude-opus-4.8",
        "input_tokens": 100,
        "output_tokens": 50,
        "upstream_cost": "1.00",
        "request_status": "success",
    }
    body.update(top)
    body["metadata"] = md
    return body


class CallbackTests(unittest.TestCase):
    def setUp(self):
        _configure()
        from fastapi.testclient import TestClient
        from gnsis.service.api import app

        self.client = TestClient(app)

    def _post(self, body, secret=SECRET):
        headers = {"Authorization": f"Bearer {secret}"} if secret else {}
        return self.client.post(PATH, json=body, headers=headers)

    # -- authentication ---------------------------------------------------
    def test_missing_secret_rejected(self):
        self.assertEqual(self._post(_body(), secret=None).status_code, 401)

    def test_wrong_secret_rejected(self):
        self.assertEqual(self._post(_body(), secret="nope").status_code, 401)

    def test_valid_secret_accepted(self):
        r = self._post(_body())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["accepted"])
        self.assertFalse(r.json()["duplicate"])

    # -- idempotency ------------------------------------------------------
    def test_duplicate_callback_is_idempotent(self):
        from gnsis.service.usage import UsageStore

        self.assertEqual(self._post(_body(rid="dup")).status_code, 200)
        second = self._post(_body(rid="dup"))
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()["duplicate"])
        self.assertEqual(UsageStore().count_for_workspace("ws-1"), 1)

    def test_duplicate_does_not_overwrite(self):
        from gnsis.service.usage import UsageStore

        self._post(_body(rid="dup2", input_tokens=100))
        first = UsageStore().get_by_litellm_id("dup2")
        # A second callback with different numbers must not change the stored fact.
        self._post(_body(rid="dup2", input_tokens=999))
        again = UsageStore().get_by_litellm_id("dup2")
        self.assertEqual(again.input_tokens, first.input_tokens)
        self.assertEqual(again.id, first.id)

    # -- persistence ------------------------------------------------------
    def test_tokens_and_cost_persisted_exactly(self):
        from gnsis.service.usage import UsageStore

        self._post(_body(rid="c1", input_tokens=1234, output_tokens=77, upstream_cost="0.00012345"))
        rec = UsageStore().get_by_litellm_id("c1")
        self.assertEqual(rec.input_tokens, 1234)
        self.assertEqual(rec.output_tokens, 77)
        self.assertEqual(rec.upstream_cost, "0.00012345")  # exact decimal string
        self.assertEqual(rec.upstream_cost_decimal, Decimal("0.00012345"))
        self.assertEqual(rec.provider, "anthropic")

    def test_failed_request_is_persisted_and_visible(self):
        from gnsis.service.usage import UsageStore

        self._post(_body(rid="f1", request_status="failure", input_tokens=0, output_tokens=0, upstream_cost="0"))
        rec = UsageStore().get_by_litellm_id("f1")
        self.assertIsNotNone(rec)
        self.assertEqual(rec.request_status, "failure")

    def test_retry_is_distinguishable_from_original(self):
        from gnsis.service.usage import UsageStore

        self._post(_body(rid="orig"))
        self._post(_body(rid="retry", retry_of="orig"))
        original = UsageStore().get_by_litellm_id("orig")
        retry = UsageStore().get_by_litellm_id("retry")
        self.assertIsNone(original.retry_of)
        self.assertEqual(retry.retry_of, "orig")
        self.assertNotEqual(original.id, retry.id)

    # -- attribution ------------------------------------------------------
    def test_native_run_linkage(self):
        from gnsis.service.usage import UsageStore

        self._post(_body(
            rid="native",
            metadata={"run_id": "job_abc", "trace_event_id": "ev_1", "repository_id": "repo_9", "phase": "implementation"},
        ))
        rec = UsageStore().get_by_litellm_id("native")
        self.assertEqual(rec.run_id, "job_abc")
        self.assertEqual(rec.trace_event_id, "ev_1")
        self.assertEqual(rec.repository_id, "repo_9")
        self.assertEqual(rec.phase, "implementation")

    def test_virtual_key_linkage(self):
        from gnsis.service.usage import UsageStore

        self._post(_body(
            rid="vk",
            metadata={"application_name": "support-bot", "team_id": "team_1", "environment": "prod"},
        ))
        rec = UsageStore().get_by_litellm_id("vk")
        self.assertEqual(rec.application_name, "support-bot")
        self.assertEqual(rec.team_id, "team_1")
        self.assertIsNone(rec.run_id)

    def test_missing_workspace_rejected(self):
        body = _body(rid="bad")
        body["metadata"].pop("workspace_id")
        self.assertEqual(self._post(body).status_code, 400)

    # -- isolation --------------------------------------------------------
    def test_cross_workspace_isolation(self):
        from gnsis.service.usage import UsageStore

        self._post(_body(rid="a", workspace="ws-A"))
        self._post(_body(rid="b", workspace="ws-B"))
        store = UsageStore()
        a_ids = {r.litellm_request_id for r in store.list_for_workspace("ws-A")}
        b_ids = {r.litellm_request_id for r in store.list_for_workspace("ws-B")}
        self.assertEqual(a_ids, {"a"})
        self.assertEqual(b_ids, {"b"})


class GatewayMetadataTests(unittest.TestCase):
    """The gateway attaches deterministic attribution (no fuzzy correlation)."""

    def setUp(self):
        _configure()

    def test_build_metadata_is_deterministic_and_id_based(self):
        from gnsis.service.settings import get_settings
        from gnsis.service.workspaces import get_or_create_workspace
        from gnsis.service.executor.gateway import build_litellm_metadata

        ws = get_or_create_workspace("owner-subject-42")
        run = types.SimpleNamespace(
            workspace_id=ws.id, job_id="job_xyz", repository_id="repo_1"
        )
        body = {"metadata": {"phase": "planning", "engine": "gnsis"}}
        md = build_litellm_metadata(get_settings(), run, body, event_id="ev_deterministic")

        self.assertEqual(md["workspace_id"], ws.id)
        self.assertEqual(md["user_id"], "owner-subject-42")  # exact owner subject
        self.assertEqual(md["run_id"], "job_xyz")
        self.assertEqual(md["repository_id"], "repo_1")
        self.assertEqual(md["model_call_event_id"], "ev_deterministic")
        self.assertEqual(md["trace_event_id"], "ev_deterministic")
        self.assertEqual(md["phase"], "planning")
        # Same inputs -> same metadata (no timestamps / ordering).
        md2 = build_litellm_metadata(get_settings(), run, body, event_id="ev_deterministic")
        self.assertEqual(md, md2)


if __name__ == "__main__":
    unittest.main()
