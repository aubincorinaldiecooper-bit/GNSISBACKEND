"""The pod's idle watcher stops a pod nobody is using, but never one that is
still booting or serving a live session."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runpod"))
import idle_stop  # noqa: E402


class IdleTests(unittest.TestCase):
    def test_not_answering_yet_is_not_idle(self):
        self.assertFalse(idle_stop.is_idle(None))

    def test_a_loading_model_is_not_idle(self):
        self.assertFalse(idle_stop.is_idle({"status": "loading", "runtime_state": "loading"}))

    def test_a_live_session_is_not_idle(self):
        health = {"runtime_state": "ready", "active_sessions": ["s1"], "busy": False}
        self.assertFalse(idle_stop.is_idle(health))

    def test_model_work_in_progress_is_not_idle(self):
        health = {"runtime_state": "ready", "active_sessions": [], "busy": True}
        self.assertFalse(idle_stop.is_idle(health))

    def test_ready_with_no_session_is_idle(self):
        health = {"runtime_state": "ready", "active_sessions": [], "busy": False}
        self.assertTrue(idle_stop.is_idle(health))

    def test_a_failed_start_is_idle_so_it_stops_billing(self):
        self.assertTrue(idle_stop.is_idle({"status": "failed", "runtime_state": "failed"}))

    def test_without_pod_credentials_it_reports_instead_of_pretending(self):
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(idle_stop.stop_pod())


if __name__ == "__main__":
    unittest.main()
