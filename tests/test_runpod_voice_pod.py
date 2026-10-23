"""The RunPod pod script: what it sends when creating the voice pod, and that
running it twice never creates a second (billed) pod."""

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import runpod_voice_pod as pod  # noqa: E402

SECRET = "edge-secret-value-never-printed"


class CreatePodTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        patcher = mock.patch.dict(
            "os.environ", {"GITHUB_REPOSITORY_OWNER": "Owner", "IMAGE_TAG": "abc123"}, clear=False
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_runpod(self, existing):
        def call(method, route, body=None):
            self.calls.append((method, route, body))
            if (method, route) == ("GET", "/pods"):
                return existing
            if (method, route) == ("POST", "/pods"):
                return {"id": "pod123"}
            raise AssertionError(f"unexpected call {method} {route}")

        return call

    def run_create(self, existing, env=None):
        env = env or {"GNSIS_EDGE_SECRET": SECRET, "MODAL_TOKEN_ID": "a", "MODAL_TOKEN_SECRET": "b"}
        out = io.StringIO()
        with mock.patch.object(pod, "runpod", self.fake_runpod(existing)), mock.patch.object(
            pod, "worker_environment", return_value=env
        ), mock.patch.object(pod, "registry_auth_id", return_value=None), redirect_stdout(out):
            pod.create()
        return out.getvalue()

    def test_creates_the_voice_pod_like_the_modal_app(self):
        printed = self.run_create(existing=[])
        posts = [body for method, route, body in self.calls if (method, route) == ("POST", "/pods")]
        self.assertEqual(len(posts), 1)
        body = posts[0]
        self.assertEqual(body["imageName"], "ghcr.io/owner/gnsis-voice:abc123")
        self.assertEqual((body["gpuTypeIds"], body["gpuCount"]), (["NVIDIA L40S"], 2))
        self.assertEqual(body["volumeMountPath"], "/models")
        self.assertEqual(body["ports"], ["7975/http"])
        self.assertEqual(body["env"]["GNSIS_EDGE_SECRET"], SECRET)
        self.assertNotIn(SECRET, printed)
        self.assertIn("https://pod123-7975.proxy.runpod.net", printed)

    def test_an_existing_pod_is_never_duplicated(self):
        with mock.patch.object(pod, "status"):
            self.run_create(existing=[{"id": "old", "name": "gnsis-voice"}])
        self.assertFalse([c for c in self.calls if c[:2] == ("POST", "/pods")])

    def test_falls_back_to_community_when_secure_has_no_capacity(self):
        tried = []

        def call(method, route, body=None):
            if (method, route) == ("GET", "/pods"):
                return []
            if (method, route) == ("POST", "/pods"):
                tried.append(body["cloudType"])
                if body["cloudType"] == "SECURE":
                    raise pod.PodError("RunPod POST /pods -> HTTP 500: no instances currently available")
                return {"id": "pod-community"}
            raise AssertionError(route)

        out = io.StringIO()
        with mock.patch.object(pod, "runpod", call), mock.patch.object(
            pod, "worker_environment", return_value={"GNSIS_EDGE_SECRET": SECRET}
        ), mock.patch.object(pod, "registry_auth_id", return_value=None), redirect_stdout(out):
            pod.create()
        self.assertEqual(tried, ["SECURE", "COMMUNITY"])
        self.assertIn("pod-community", out.getvalue())

    def test_drops_simplemem_url_when_its_token_did_not_copy(self):
        captured = {}

        def call(method, route, body=None):
            if (method, route) == ("GET", "/pods"):
                return []
            if (method, route) == ("POST", "/pods"):
                captured["env"] = body["env"]
                return {"id": "p"}
            raise AssertionError(route)

        env = {"GNSIS_EDGE_SECRET": SECRET, "GNSIS_SIMPLEMEM_URL": "https://simplemem.example.com"}
        with mock.patch.object(pod, "runpod", call), mock.patch.object(
            pod, "worker_environment", return_value=env
        ), mock.patch.object(pod, "registry_auth_id", return_value=None), redirect_stdout(io.StringIO()):
            pod.create()
        self.assertNotIn("GNSIS_SIMPLEMEM_URL", captured["env"])

    def test_two_pods_with_the_name_refuse_to_guess(self):
        with mock.patch.object(pod, "runpod", return_value=[{"name": "gnsis-voice"}] * 2):
            with self.assertRaises(pod.PodError):
                pod.find_pod()


class DeployTests(unittest.TestCase):
    def run_deploy(self, existing):
        calls = []

        def call(method, route, body=None):
            calls.append((method, route, body))
            if (method, route) == ("GET", "/pods"):
                return existing
            if method == "PATCH":
                return {}
            raise AssertionError(f"unexpected call {method} {route}")

        env = {"GITHUB_REPOSITORY_OWNER": "Owner", "IMAGE_TAG": "abc123def456"}
        with mock.patch.dict("os.environ", env), mock.patch.object(pod, "runpod", call), redirect_stdout(io.StringIO()):
            pod.deploy()
        return [c for c in calls if c[0] == "PATCH"]

    def test_moves_the_pod_to_the_exact_build(self):
        patches = self.run_deploy([{"id": "p1", "name": "gnsis-voice", "imageName": "ghcr.io/owner/gnsis-voice:latest"}])
        self.assertEqual(patches, [("PATCH", "/pods/p1", {"imageName": "ghcr.io/owner/gnsis-voice:abc123def456"})])

    def test_no_restart_when_already_on_that_build(self):
        same = [{"id": "p1", "name": "gnsis-voice", "imageName": "ghcr.io/owner/gnsis-voice:abc123def456"}]
        self.assertEqual(self.run_deploy(same), [])

    def test_no_pod_is_not_an_error(self):
        self.assertEqual(self.run_deploy([]), [])


if __name__ == "__main__":
    unittest.main()
