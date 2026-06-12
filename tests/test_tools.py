import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from gnsis.tools import default_registry, safe_arithmetic  # noqa: E402


class ToolTests(unittest.TestCase):
    def test_calculator_is_correct(self):
        registry = default_registry()
        result = registry.run("calculator", {"expression": "(12 + 30) * 2"})
        self.assertFalse(result.is_error)
        self.assertEqual(result.content, "84")

    def test_calculator_integer_rendering(self):
        self.assertEqual(default_registry().run("calculator", {"expression": "10 / 2"}).content, "5")

    def test_safe_arithmetic_rejects_names_and_calls(self):
        with self.assertRaises(Exception):
            safe_arithmetic("__import__('os').system('echo hi')")
        with self.assertRaises(Exception):
            safe_arithmetic("len('abc')")

    def test_calculator_reports_errors_gracefully(self):
        result = default_registry().run("calculator", {"expression": "1 +"})
        self.assertTrue(result.is_error)

    def test_unknown_tool(self):
        result = default_registry().run("nope", {})
        self.assertTrue(result.is_error)

    def test_string_reverse(self):
        self.assertEqual(default_registry().run("string_reverse", {"text": "abc"}).content, "cba")

    def test_specs_are_openai_shaped(self):
        specs = default_registry().specs()
        self.assertTrue(all(s["type"] == "function" for s in specs))
        names = {s["function"]["name"] for s in specs}
        self.assertIn("calculator", names)


if __name__ == "__main__":
    unittest.main()
