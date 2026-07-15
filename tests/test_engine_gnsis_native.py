"""End-to-end test of GnsisNativeEngine against a real (temp) git workspace,
driven by a scripted fake model so no network call happens."""

import os
import subprocess
import sys
import tempfile
import unittest
from typing import Any, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from gnsis.engines.gnsis_native import GnsisNativeEngine  # noqa: E402
from gnsis.models.base import BaseModel, Message, ModelResponse, ToolCall  # noqa: E402
from gnsis.orchestration.engine import Workspace  # noqa: E402
from gnsis.orchestration.status import Phase  # noqa: E402


class ScriptedModel(BaseModel):
    """Returns pre-scripted responses in order; the last one repeats if exhausted."""

    provider = "scripted"
    model = "scripted-1"

    def __init__(self, responses: List[ModelResponse]) -> None:
        self.responses = responses
        self.calls = 0

    def generate(
        self, messages: List[Message], tools: Optional[List[dict]] = None, **kwargs: Any
    ) -> ModelResponse:
        idx = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return self.responses[idx]


class RecordingSink:
    """A PhaseSink that records everything instead of touching a store."""

    def __init__(self) -> None:
        self.phases: List[str] = []
        self.checkpoints: List[tuple] = []
        self.logs: List[tuple] = []

    def begin_phase(self, phase: str) -> None:
        self.phases.append(phase)

    def checkpoint(self, phase: str, content: Any) -> None:
        self.checkpoints.append((phase, content))

    def log(self, message: str, level: str = "info", **data: Any) -> None:
        self.logs.append((level, message))


def _init_git_repo(path: str, filename: str, content: str) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    with open(os.path.join(path, filename), "w") as f:
        f.write(content)
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)


class GnsisNativeEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = self.tmp.name
        _init_git_repo(self.path, "app.py", "def greet():\n    return 'hi'\n")
        self.workspace = Workspace(path=self.path, repo="o/r", base_branch="main")

    def tearDown(self):
        self.tmp.cleanup()

    def _run_with_scripted_responses(self, responses: List[ModelResponse]):
        import gnsis.engines.gnsis_native as mod

        fake_model = ScriptedModel(responses)
        original = mod.OpenRouterModel
        # Every OpenRouterModel(...) construction in the engine returns this
        # same scripted instance, regardless of the kwargs it was called with.
        mod.OpenRouterModel = lambda *args, **kwargs: fake_model
        try:
            engine = GnsisNativeEngine(model="fake/model")
            sink = RecordingSink()
            result = engine.generate("Change the greeting to 'hello'", self.workspace, sink)
            return result, sink, fake_model
        finally:
            mod.OpenRouterModel = original

    def test_engine_edits_file_and_produces_diff(self):
        tool_call_response = ModelResponse(
            text="",
            tool_calls=[
                ToolCall(
                    id="call_1",
                    name="edit_file",
                    arguments={"path": "app.py", "old_string": "hi", "new_string": "hello"},
                )
            ],
            finish_reason="tool_calls",
        )
        final_response = ModelResponse(text="Done — updated the greeting.", finish_reason="stop")
        summary_response = ModelResponse(text="Changed the greeting from 'hi' to 'hello'.")

        result, sink, model = self._run_with_scripted_responses(
            [tool_call_response, final_response, summary_response]
        )

        self.assertTrue(result.success)
        self.assertIn("app.py", result.files_changed)
        with open(os.path.join(self.path, "app.py")) as f:
            self.assertIn("hello", f.read())
        self.assertIn("+", result.patch)
        self.assertEqual(result.summary, "Changed the greeting from 'hi' to 'hello'.")
        self.assertEqual(result.detail["engine"], "gnsis")

        # Phase order: plan, patch, tests, summary.
        self.assertEqual(sink.phases, [Phase.PLAN, Phase.PATCH, Phase.TESTS, Phase.SUMMARY])

        # Per-step tracer events reached the sink live, not just phase checkpoints.
        joined_logs = " ".join(msg for _, msg in sink.logs)
        self.assertIn("tool edit_file", joined_logs)
        self.assertIn("agent finished", joined_logs)

    def test_no_changes_reports_failure_without_running_tests(self):
        final_response = ModelResponse(text="Nothing to change here.", finish_reason="stop")
        result, sink, model = self._run_with_scripted_responses([final_response])

        self.assertFalse(result.success)
        self.assertNotIn(Phase.TESTS, sink.phases)

    def test_boundary_violation_surfaces_as_tool_error_not_a_crash(self):
        tool_call_response = ModelResponse(
            text="",
            tool_calls=[
                ToolCall(id="call_1", name="read_file", arguments={"path": "../../etc/passwd"})
            ],
            finish_reason="tool_calls",
        )
        final_response = ModelResponse(text="Could not read that file.", finish_reason="stop")
        summary_response = ModelResponse(text="No changes were made.")

        # No file gets edited, so this ends as "no changes" — the important
        # assertion is that the boundary violation didn't raise/crash the run.
        result, sink, model = self._run_with_scripted_responses(
            [tool_call_response, final_response, summary_response]
        )
        self.assertFalse(result.success)
        joined_logs = " ".join(msg for _, msg in sink.logs)
        self.assertIn("error", joined_logs)


if __name__ == "__main__":
    unittest.main()
