"""Offline checks for the Coolify API migration gate."""
import importlib.util
import pathlib
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "coolify_inventory.py"
SPEC = importlib.util.spec_from_file_location("coolify_inventory", SCRIPT)
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)


class FakeClient:
    def __init__(self, duplicate=False):
        self.calls = []
        self.apps = [
            {
                "uuid": "api123",
                "name": "g-n-s-i-s-b-a-c-k-e-n-d:main",
                "git_repository": "aubincorinaldiecooper-bit/GNSISBACKEND",
                "environment_id": 10,
                "destination_id": 20,
                "status": "running:healthy",
            },
            {
                "uuid": "front123",
                "name": "gnsisfrontend",
                "git_repository": "aubincorinaldiecooper-bit/GNSISFRONTEND",
            },
        ]
        if duplicate:
            self.apps.append(
                {
                    "uuid": "api456",
                    "name": "gnsisbackend",
                    "git_repository": "aubincorinaldiecooper-bit/GNSISBACKEND",
                }
            )

    def request(self, method, route):
        self.calls.append((method, route))
        assert method == "GET"
        if route == "/applications":
            return self.apps
        if route == "/databases":
            return [
                {
                    "uuid": "pg123",
                    "name": "GNSIS POSTGRES",
                    "image": "postgres:18-alpine",
                    "status": "running:healthy",
                    "environment_id": 10,
                    "destination_id": 20,
                }
            ]
        if route == "/applications/api123/envs":
            # Intentionally missing DATABASE_URL; no values are ever inspected.
            return [
                {"key": name, "value": "not-for-logs"}
                for name in migration.API_ENV_KEYS - {"DATABASE_URL"}
            ]
        if route == "/databases/pg123/storages":
            return {"persistent_storages": [{"mount_path": "/var/lib/postgresql"}]}
        raise AssertionError("Unexpected API route")


class CoolifyMigrationTests(unittest.TestCase):
    def test_discovers_backend_and_preserves_secret_values(self):
        client = FakeClient()
        result = migration.inventory(client, "")
        self.assertEqual(result["app_uuid"], "api123")
        self.assertEqual(result["missing_keys"], ["DATABASE_URL"])
        self.assertTrue(result["pg_volume_ok"])
        self.assertNotIn("not-for-logs", "\n".join(migration.summary_lines(result, [])))
        self.assertTrue(all(method == "GET" for method, _ in client.calls))

    def test_refuses_ambiguous_backend(self):
        with self.assertRaises(ValueError):
            migration.select_backend(FakeClient(duplicate=True).apps)
        self.assertEqual(
            migration.select_backend(FakeClient(duplicate=True).apps, "api123")["uuid"],
            "api123",
        )

    def test_gates_deploy_on_migration_and_missing_configuration(self):
        result = migration.inventory(FakeClient(), "")
        self.assertTrue(migration.blockers(result, False))
        self.assertTrue(migration.blockers(result, True))
        result["missing_keys"] = []
        result["redis_running"] = True
        self.assertFalse(migration.blockers(result, True))
        result["pg_environment_id"] = "different"
        self.assertTrue(migration.blockers(result, True))


if __name__ == "__main__":
    unittest.main()
