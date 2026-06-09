import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from gnsis import (  # noqa: E402
    Config,
    PromptOptimizer,
    Runtime,
    SelfEvolutionLoop,
    calculator_task,
)


class EvolutionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.runtime = Runtime(Config({"provider": "mock", "workdir": self._tmp.name}))
        self.loop = SelfEvolutionLoop(
            self.runtime, optimizer=PromptOptimizer(model=self.runtime.model)
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_loop_improves_from_zero_to_perfect(self):
        report = self.loop.run(calculator_task("(12 + 30) * 2"), iterations=5)
        self.assertEqual(report.start_score, 0.0)
        self.assertEqual(report.best_score, 1.0)
        self.assertTrue(report.improved)
        self.assertIn("tool", report.best_prompt.lower())

    def test_loop_commits_versioned_lineage(self):
        self.loop.run(calculator_task("(12 + 30) * 2"), iterations=5)
        history = self.runtime.store.history("prompt", "agent_system_prompt")
        self.assertGreaterEqual(len(history), 2)
        # The evolved head should descend from the seed.
        self.assertIsNone(history[0].parent_version)
        self.assertEqual(history[-1].parent_version, history[-2].version)

    def test_remembers_each_step(self):
        self.loop.run(calculator_task("(12 + 30) * 2"), iterations=5)
        events = self.runtime.memory.recall()
        self.assertGreaterEqual(len(events), 2)
        self.assertTrue(all(e["kind"] == "evolution_step" for e in events))

    def test_second_run_is_already_optimal(self):
        self.loop.run(calculator_task("(12 + 30) * 2"), iterations=5)
        # Starting again from the evolved head: nothing left to improve.
        report = self.loop.run(calculator_task("(12 + 30) * 2"), iterations=5)
        self.assertEqual(report.start_score, 1.0)
        self.assertEqual(report.best_score, 1.0)

    def test_rollback_restores_seed(self):
        self.loop.run(calculator_task("(12 + 30) * 2"), iterations=5)
        seed = self.runtime.store.get("prompt", "agent_system_prompt", 1).content
        rolled = self.runtime.store.rollback("prompt", "agent_system_prompt", to_version=1)
        self.assertEqual(rolled.content, seed)


if __name__ == "__main__":
    unittest.main()
