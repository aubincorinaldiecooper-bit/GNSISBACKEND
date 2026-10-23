"""Omni-SimpleMem is the required GNSIS recall surface: configuring its URL must
select it, an explicit GNSIS_MEMORY must still win, and a deployment without it
must be visibly Postgres-only rather than silently "complete"."""

import os
import unittest
from unittest import mock

from gnsis.service.settings import Settings

BASE_ENV = {"DATABASE_URL": "postgresql://u:p@localhost/db", "REDIS_URL": "redis://localhost:6379/0"}


def _settings(**env):
    with mock.patch.dict(os.environ, {**BASE_ENV, **env}, clear=True):
        return Settings.from_env()


class MemoryBackendSelectionTests(unittest.TestCase):
    def test_simplemem_url_selects_simplemem(self):
        s = _settings(GNSIS_SIMPLEMEM_URL="https://simplemem.example.com")
        self.assertEqual(s.memory_backend, "simplemem")
        self.assertEqual(s.simplemem_url, "https://simplemem.example.com")

    def test_without_simplemem_url_falls_back_to_postgres(self):
        s = _settings()
        self.assertEqual(s.memory_backend, "postgres")
        self.assertIsNone(s.simplemem_url)

    def test_blank_simplemem_url_does_not_select_simplemem(self):
        s = _settings(GNSIS_SIMPLEMEM_URL="   ")
        self.assertEqual(s.memory_backend, "postgres")
        self.assertIsNone(s.simplemem_url)

    def test_explicit_backend_overrides_auto_selection(self):
        s = _settings(GNSIS_SIMPLEMEM_URL="https://simplemem.example.com", GNSIS_MEMORY="postgres")
        self.assertEqual(s.memory_backend, "postgres")
        # The mirror into SimpleMem still has its URL.
        self.assertEqual(s.simplemem_url, "https://simplemem.example.com")


class SimpleMemTokenTests(unittest.TestCase):
    def test_api_and_worker_honor_custom_token_env(self):
        # GNSIS_SIMPLEMEM_TOKEN_ENV names the secret; the default variable is unset.
        env = {
            **BASE_ENV,
            "GNSIS_SIMPLEMEM_URL": "https://simplemem.example.com",
            "GNSIS_SIMPLEMEM_TOKEN_ENV": "CUSTOM_SIMPLEMEM_SECRET",
            "CUSTOM_SIMPLEMEM_SECRET": "c" * 40,
        }
        with mock.patch.dict(os.environ, env, clear=True):
            settings = Settings.from_env()
            from gnsis.service import api

            with mock.patch.object(api, "get_settings", return_value=settings):
                provider = api._memory()
        self.assertEqual(provider.token, "c" * 40)
        self.assertEqual(provider.url, "https://simplemem.example.com")


class HealthReportsSimpleMemTests(unittest.TestCase):
    def _health(self, settings):
        from gnsis.service import api

        with mock.patch.object(api, "get_settings", return_value=settings):
            return api.health()

    def test_health_flags_missing_simplemem(self):
        payload = self._health(_settings())
        self.assertEqual(payload["memory"], "postgres")
        self.assertEqual(payload["simplemem"], "not_configured")

    def test_health_degrades_when_configured_simplemem_is_unreachable(self):
        # Port 9 (discard) on localhost: nothing answers, so readiness fails.
        payload = self._health(
            _settings(
                GNSIS_SIMPLEMEM_URL="http://127.0.0.1:9",
                GNSIS_SIMPLEMEM_INTERNAL_TOKEN="t" * 40,
            )
        )
        self.assertEqual(payload["simplemem"], "configured")
        self.assertEqual(payload["memory"], "unavailable")
        self.assertEqual(payload["status"], "degraded")


if __name__ == "__main__":
    unittest.main()
