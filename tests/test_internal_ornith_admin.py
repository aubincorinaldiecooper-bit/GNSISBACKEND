"""The internal routes that publish and locate Ornith, the live runtime's brain.

Same boundary as the Gander routes: the API holds no Modal credentials, it only
puts work on the worker's queue, and publishing needs the app named back.
"""

from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from _authkit import fresh_sqlite_env  # noqa: E402


def _prepare():
    os.environ["GNSIS_API_KEY"] = "secret-key"
    fresh_sqlite_env()
    from gnsis.service import settings as sm

    sm._settings = None
    from gnsis.service.db import init_db

    init_db()


class InternalOrnithAdminTests(unittest.TestCase):
    def setUp(self):
        _prepare()
        from fastapi.testclient import TestClient
        from gnsis.service import api

        self.api = api
        self.client = TestClient(api.app)
        self.auth = {"Authorization": "Bearer secret-key"}

    def test_status_requires_internal_key(self):
        r = self.client.post("/internal/compute/ornith/status")
        self.assertEqual(r.status_code, 401)

    def test_deploy_requires_internal_key(self):
        with patch("gnsis.service.tasks.deploy_ornith_brain.delay") as delay:
            r = self.client.post(
                "/internal/compute/ornith/deploy",
                json={"confirm": "gnsis-ornith"},
            )
        self.assertEqual(r.status_code, 401)
        delay.assert_not_called()

    def test_status_queues_worker_task(self):
        queued = SimpleNamespace(id="task-ornith-status")
        with patch("gnsis.service.tasks.ornith_status.delay", return_value=queued) as delay:
            r = self.client.post("/internal/compute/ornith/status", headers=self.auth)
        self.assertEqual(r.status_code, 202, r.text)
        self.assertEqual(r.json()["task_id"], "task-ornith-status")
        self.assertEqual(r.json()["operation"], "ornith_status")
        delay.assert_called_once_with()

    def test_deploy_requires_exact_confirmation(self):
        with patch("gnsis.service.tasks.deploy_ornith_brain.delay") as delay:
            r = self.client.post(
                "/internal/compute/ornith/deploy",
                headers=self.auth,
                json={"confirm": "gnsis-live"},
            )
        self.assertEqual(r.status_code, 409)
        delay.assert_not_called()

    def test_deploy_queues_worker_task(self):
        queued = SimpleNamespace(id="task-ornith-deploy")
        with patch(
            "gnsis.service.tasks.deploy_ornith_brain.delay", return_value=queued
        ) as delay:
            r = self.client.post(
                "/internal/compute/ornith/deploy",
                headers=self.auth,
                json={"confirm": "gnsis-ornith"},
            )
        self.assertEqual(r.status_code, 202, r.text)
        body = r.json()
        self.assertEqual(body["task_id"], "task-ornith-deploy")
        self.assertEqual(body["app"], "gnsis-ornith")
        delay.assert_called_once_with()

    def test_the_api_never_holds_modal_credentials(self):
        """The route enqueues and returns; it must not build a Modal client."""

        queued = SimpleNamespace(id="task-ornith-status")
        with patch("gnsis.service.modal_compute.from_settings") as boundary:
            with patch("gnsis.service.tasks.ornith_status.delay", return_value=queued):
                r = self.client.post("/internal/compute/ornith/status", headers=self.auth)
        self.assertEqual(r.status_code, 202, r.text)
        boundary.assert_not_called()


if __name__ == "__main__":
    unittest.main()
