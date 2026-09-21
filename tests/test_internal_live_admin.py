from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from _authkit import fresh_sqlite_env  # noqa: E402


def _prepare():
    os.environ["GNSIS_API_KEY"] = "secret-key"
    fresh_sqlite_env()
    from gnsis.service import settings as sm

    sm._settings = None
    from gnsis.service.db import init_db

    init_db()


class InternalGNSISAdminTests(unittest.TestCase):
    def setUp(self):
        _prepare()
        from fastapi.testclient import TestClient
        from gnsis.service import api

        self.api = api
        self.client = TestClient(api.app)
        self.auth = {"Authorization": "Bearer secret-key"}

    def test_status_requires_internal_key(self):
        r = self.client.post("/internal/compute/live/status")
        self.assertEqual(r.status_code, 401)

    def test_status_queues_worker_task(self):
        queued = SimpleNamespace(id="task-status")
        with patch("gnsis.service.tasks.modal_gnsis_status.delay", return_value=queued) as delay:
            r = self.client.post(
                "/internal/compute/live/status?smoke=false",
                headers=self.auth,
            )
        self.assertEqual(r.status_code, 202, r.text)
        self.assertEqual(r.json()["task_id"], "task-status")
        delay.assert_called_once_with(smoke=False)

    def test_deploy_requires_exact_confirmation(self):
        with patch("gnsis.service.tasks.deploy_live_runtime.delay") as delay:
            r = self.client.post(
                "/internal/compute/live/deploy",
                headers=self.auth,
                json={"confirm": "wrong", "smoke": False},
            )
        self.assertEqual(r.status_code, 409)
        delay.assert_not_called()

    def test_deploy_queues_worker_task(self):
        queued = SimpleNamespace(id="task-deploy")
        with patch("gnsis.service.tasks.deploy_live_runtime.delay", return_value=queued) as delay:
            r = self.client.post(
                "/internal/compute/live/deploy",
                headers=self.auth,
                json={"confirm": "gnsis-live", "smoke": True},
            )
        self.assertEqual(r.status_code, 202, r.text)
        self.assertEqual(r.json()["task_id"], "task-deploy")
        delay.assert_called_once_with(smoke=True)

    def test_poll_success_projects_only_safe_fields(self):
        async_result = Mock()
        async_result.state = "SUCCESS"
        async_result.ready.return_value = True
        async_result.successful.return_value = True
        async_result.result = {
            "app": "gnsis-live",
            "environment": "main",
            "url": "https://example.modal.run",
            "health": {"status": "ok"},
            "secret": "must-not-leak",
        }
        with patch.object(self.api, "_compute_task_response", wraps=self.api._compute_task_response):
            with patch("gnsis.service.tasks.celery_app.AsyncResult", return_value=async_result):
                r = self.client.get("/internal/compute/tasks/task-1", headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertNotIn("secret", data["result"])
        self.assertEqual(data["result"]["app"], "gnsis-live")

    def test_poll_failure_does_not_return_exception_text(self):
        async_result = Mock()
        async_result.state = "FAILURE"
        async_result.ready.return_value = True
        async_result.successful.return_value = False
        async_result.result = RuntimeError("token=super-secret")
        with patch("gnsis.service.tasks.celery_app.AsyncResult", return_value=async_result):
            r = self.client.get("/internal/compute/tasks/task-2", headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["error"], "task failed; inspect GNSISWORKER logs")
        self.assertNotIn("super-secret", r.text)


if __name__ == "__main__":
    unittest.main()
