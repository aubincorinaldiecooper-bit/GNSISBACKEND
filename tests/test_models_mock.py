import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from gnsis.models import MockModel, Message  # noqa: E402
from gnsis.tools import default_registry  # noqa: E402


class MockModelTests(unittest.TestCase):
    def setUp(self):
        self.model = MockModel()
        self.tools = default_registry().specs()

    def test_uses_tool_when_prompt_requires_it(self):
        messages = [
            Message.system("Always use the calculator tool to compute answers."),
            Message.user("What is (12 + 30) * 2?"),
        ]
        response = self.model.generate(messages, tools=self.tools)
        self.assertEqual(len(response.tool_calls), 1)
        call = response.tool_calls[0]
        self.assertEqual(call.name, "calculator")
        self.assertEqual(call.arguments["expression"], "(12 + 30) * 2")

    def test_guesses_without_tool_instruction(self):
        messages = [
            Message.system("You are a helpful assistant."),
            Message.user("What is (12 + 30) * 2?"),
        ]
        response = self.model.generate(messages, tools=self.tools)
        self.assertEqual(response.tool_calls, [])
        self.assertNotIn("84", response.text)

    def test_folds_tool_result_into_final_answer(self):
        messages = [
            Message.system("Always use the calculator tool."),
            Message.user("What is (12 + 30) * 2?"),
            Message.assistant("", []),
            Message.tool("call_1", "84", name="calculator"),
        ]
        response = self.model.generate(messages, tools=self.tools)
        self.assertEqual(response.tool_calls, [])
        self.assertIn("84", response.text)

    def test_is_deterministic(self):
        messages = [Message.system("use tools"), Message.user("What is 2 + 2?")]
        a = self.model.generate(messages, tools=self.tools)
        b = self.model.generate(messages, tools=self.tools)
        self.assertEqual(a.tool_calls[0].arguments, b.tool_calls[0].arguments)


if __name__ == "__main__":
    unittest.main()
