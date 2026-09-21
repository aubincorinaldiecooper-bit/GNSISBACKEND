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
        assert function_name == "gnsis_live_server"
        assert environment_name == "main"
        return cls()

    def hydrate(self):
        type(self).hydrated += 1

    def get_web_url(self):
        return "https://gnsis-live.example/"


class _FakeModal:
    Function = _FakeFunction


class ModalComputeTests(unittest.TestCase):
    def test_discovers_gnsis_without_leaking_credentials_into_process(self):
        old_id = os.environ.pop("MODAL_TOKEN_ID", None)
        old_secret = os.environ.pop("MODAL_TOKEN_SECRET", None)
        try:
            compute = ModalCompute(
                token_id="id-test",
                token_secret="secret-test",
                modal_module=_FakeModal,
            )
            self.assertEqual(compute.gnsis_web_url(), "https://gnsis-live.example")
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
            compute.deploy_gnsis(
                repo_root="/repo",
                models_volume="weights",
                secret_name="ornith",
            )
        args, kwargs = run.call_args
        self.assertEqual(args[0][-1], "modal/live.py")
        self.assertEqual(kwargs["cwd"], "/repo")
        self.assertTrue(kwargs["check"])
        self.assertEqual(kwargs["env"]["MODAL_TOKEN_ID"], "id-test")
        self.assertEqual(kwargs["env"]["MODAL_TOKEN_SECRET"], "secret-test")
        self.assertEqual(kwargs["env"]["GNSIS_MODELS_VOLUME"], "weights")
        self.assertEqual(kwargs["env"]["GNSIS_SECRET_NAME"], "ornith")

    def test_ornith_is_looked_up_as_its_own_app(self):
        """The brain is a separate app, so it must not resolve through GNSIS's ref."""

        seen = {}

        class _Fn:
            @classmethod
            def from_name(cls, app_name, function_name, environment_name=None):
                seen.update(
                    app=app_name, function=function_name, environment=environment_name
                )
                return cls()

            def hydrate(self):
                return None

            def get_web_url(self):
                return "https://gnsis-ornith.example/"

        compute = ModalCompute(
            token_id="id-test",
            token_secret="secret-test",
            modal_module=SimpleNamespace(Function=_Fn),
        )
        self.assertEqual(compute.ornith_web_url(), "https://gnsis-ornith.example")
        self.assertEqual(seen["app"], "gnsis-ornith")
        self.assertEqual(seen["function"], "ornith_server_v2")
        self.assertEqual(seen["environment"], "main")

    def test_publishing_ornith_names_its_own_app_and_resources(self):
        """A publish must land on the app the status call reads, never elsewhere."""

        compute = ModalCompute(
            token_id="id-test",
            token_secret="secret-test",
            ornith_app_name="gnsis-ornith-test",
        )
        with patch("gnsis.service.modal_compute.subprocess.run") as run:
            compute.deploy_ornith(
                repo_root="/repo",
                cache_volume="cache",
                secret_name="ornith",
            )
        args, kwargs = run.call_args
        self.assertEqual(args[0][-1], "modal/ornith.py")
        self.assertEqual(kwargs["cwd"], "/repo")
        self.assertTrue(kwargs["check"])
        self.assertEqual(kwargs["env"]["MODAL_TOKEN_ID"], "id-test")
        self.assertEqual(kwargs["env"]["ORNITH_MODAL_APP_NAME"], "gnsis-ornith-test")
        self.assertEqual(kwargs["env"]["ORNITH_CACHE_VOLUME"], "cache")
        self.assertEqual(kwargs["env"]["ORNITH_SECRET_NAME"], "ornith")
        # The image tag is only forced when a caller pins one.
        self.assertNotIn("ORNITH_VLLM_IMAGE", kwargs["env"])

    def test_the_default_ornith_app_is_not_the_running_one(self):
        """The live brain was deployed from a notebook as legacy-ornith-service.

        Defaulting to that name would let any publish from this repository
        replace a running production service, so the default is a name of its
        own and adopting the existing app has to be asked for.
        """

        compute = ModalCompute(token_id="id-test", token_secret="secret-test")
        self.assertEqual(compute.ornith_ref.app_name, "gnsis-ornith")
        self.assertNotEqual(compute.ornith_ref.app_name, "legacy-ornith-service")

    def test_settings_carry_the_ornith_app_but_never_its_function_name(self):
        """The app is a setting; the function name cannot be one.

        A Modal function is named by its `def`, so modal/ornith.py always
        publishes ornith_server_v2. A configurable lookup name would let a
        publish succeed and the very next lookup fail, so even a settings
        object that carries one must not change where the provider looks
        (Codex on #58).
        """

        settings = SimpleNamespace(
            modal_token_id="id",
            modal_token_secret="secret",
            modal_environment="main",
            gnsis_modal_app_name="gnsis-live",
            gnsis_modal_function_name="gnsis_live_server",
            ornith_modal_app_name="adopted-app",
            ornith_modal_function_name="not_a_real_function",
        )
        compute = from_settings(settings)
        self.assertEqual(compute.ornith_ref.app_name, "adopted-app")
        self.assertEqual(compute.ornith_ref.function_name, "ornith_server_v2")
        self.assertEqual(compute.ref.app_name, "gnsis-live")

    def test_the_looked_up_function_is_the_one_the_definition_publishes(self):
        """Tie the name we look up to the name Modal will actually create.

        These live in two files, and nothing but this check keeps them equal.
        """

        import ast
        from pathlib import Path

        definition = Path(__file__).resolve().parents[1] / "modal" / "ornith.py"
        published = {
            node.name
            for node in ast.parse(definition.read_text()).body
            if isinstance(node, ast.FunctionDef)
        }
        compute = ModalCompute(token_id="id-test", token_secret="secret-test")
        self.assertIn(compute.ornith_ref.function_name, published)

    def test_settings_boundary_requires_both_modal_values(self):
        settings = SimpleNamespace(
            modal_token_id=None,
            modal_token_secret=None,
            modal_environment="main",
            gnsis_modal_app_name="gnsis-live",
            gnsis_modal_function_name="gnsis_live_server",
        )
        with self.assertRaisesRegex(RuntimeError, "missing Modal credentials"):
            from_settings(settings)


if __name__ == "__main__":
    unittest.main()
