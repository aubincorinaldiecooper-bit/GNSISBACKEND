import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from gnsis.agent import ToolCallingAgent  # noqa: E402
from gnsis.models import MockModel  # noqa: E402
from gnsis.tools import default_registry  # noqa: E402
from gnsis.tracer import Tracer  # noqa: E402

STRONG = "You are precise. Always use the calculator tool to compute exact answers."
WEAK = "You are a helpful assistant. Answer the user's question."


class AgentTests(unittest.TestCase):
    def test_tool_loop_reaches_correct_answer(self):
        tracer = Tracer()
        agent = ToolCallingAgent(MockModel(), default_registry(), STRONG, tracer=tracer)
        result = agent.run("What is (12 + 30) * 2?")
        self.assertTrue(result.used_tool)
        self.assertEqual(result.tool_calls, 1)
        self.assertIn("84", result.output)
        kinds = [e.kind for e in tracer.events]
        self.assertIn("tool_call", kinds)
        self.assertIn("agent_final", kinds)

    def test_weak_prompt_does_not_use_tool(self):
        agent = ToolCallingAgent(MockModel(), default_registry(), WEAK)
        result = agent.run("What is (12 + 30) * 2?")
        self.assertFalse(result.used_tool)
        self.assertNotIn("84", result.output)


if __name__ == "__main__":
    unittest.main()
