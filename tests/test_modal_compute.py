import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from gnsis.service.modal_compute import ModalCompute, from_settings


class _FakeFunction:
    hydrated = 0

    @classmethod
    def from_name(cls, app_name, function_name, environment_name=None):
        assert app_name == "gnsis-live"
        assert function_name == "gander_server"
        assert environment_name == "main"
        return cls()

    def hydrate(self):
        type(self).hydrated += 1

    def get_web_url(self):
        return "https://gnsis-live.example/"


class _FakeModal:
    Function = _FakeFunction


class ModalComputeTests(unittest.TestCase):
    def test_discovers_gander_without_leaking_credentials_into_process(self):
        old_id = os.environ.pop("MODAL_TOKEN_ID", None)
        old_secret = os.environ.pop("MODAL_TOKEN_SECRET", None)
        try:
            compute = ModalCompute(
                token_id="id-test",
                token_secret="secret-test",
                modal_module=_FakeModal,
            )
            self.assertEqual(compute.gander_web_url(), "https://gnsis-live.example")
            self.assertNotIn("MODAL_TOKEN_ID", os.environ)
            self.assertNotIn("MODAL_TOKEN_SECRET", os.environ)
        finally:
            if old_id is not None:
                os.environ["MODAL_TOKEN_ID"] = old_id
            if old_secret is not None:
                os.environ["MODAL_TOKEN_SECRET"] = old_secret

    def test_deploy_is_explicit_and_uses_worker_credentials(self):
        compute = ModalCompute(token_id="id-test", token_secret="secret-test")
        with patch("gnsis.service.modal_compute.subprocess.run") as run:
            compute.deploy_gander(
                repo_root="/repo",
                models_volume="weights",
                secret_name="ornith",
            )
        args, kwargs = run.call_args
        self.assertEqual(args[0][-1], "modal/gander.py")
        self.assertEqual(kwargs["cwd"], "/repo")
        self.assertTrue(kwargs["check"])
        self.assertEqual(kwargs["env"]["MODAL_TOKEN_ID"], "id-test")
        self.assertEqual(kwargs["env"]["MODAL_TOKEN_SECRET"], "secret-test")
        self.assertEqual(kwargs["env"]["GANDER_MODELS_VOLUME"], "weights")
        self.assertEqual(kwargs["env"]["GANDER_SECRET_NAME"], "ornith")

    def test_settings_boundary_requires_both_modal_values(self):
        settings = SimpleNamespace(
            modal_token_id=None,
            modal_token_secret=None,
            modal_environment="main",
            gander_modal_app_name="gnsis-live",
            gander_modal_function_name="gander_server",
        )
        with self.assertRaisesRegex(RuntimeError, "missing Modal credentials"):
            from_settings(settings)


if __name__ == "__main__":
    unittest.main()
