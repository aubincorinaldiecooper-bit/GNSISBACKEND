import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from gnsis.models import Message, ToolCall  # noqa: E402
from gnsis.models.openrouter import OpenRouterModel, _message_to_payload  # noqa: E402


class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class OpenRouterMappingTests(unittest.TestCase):
    def test_user_message_payload(self):
        self.assertEqual(
            _message_to_payload(Message.user("hi")),
            {"role": "user", "content": "hi"},
        )

    def test_tool_message_payload(self):
        payload = _message_to_payload(Message.tool("call_1", "84", name="calculator"))
        self.assertEqual(payload["role"], "tool")
        self.assertEqual(payload["tool_call_id"], "call_1")
        self.assertEqual(payload["content"], "84")

    def test_assistant_with_tool_calls_payload(self):
        msg = Message.assistant("", [ToolCall("call_1", "calculator", {"expression": "2+2"})])
        payload = _message_to_payload(msg)
        self.assertEqual(payload["role"], "assistant")
        self.assertEqual(len(payload["tool_calls"]), 1)
        call = payload["tool_calls"][0]
        self.assertEqual(call["function"]["name"], "calculator")
        self.assertEqual(json.loads(call["function"]["arguments"]), {"expression": "2+2"})

    def test_missing_api_key_raises(self):
        model = OpenRouterModel(api_key=None)
        model.api_key = None  # ensure no env key leaks in
        with self.assertRaises(RuntimeError):
            model.generate([Message.user("hi")])


class OpenRouterRequestTests(unittest.TestCase):
    def setUp(self):
        self.model = OpenRouterModel(api_key="test-key", model="anthropic/claude-opus-4.8")

    def _generate(self, response_payload, **kwargs):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data.decode("utf-8"))
            captured["headers"] = dict(request.headers)
            return _FakeResponse(response_payload)

        with mock.patch("gnsis.models.openrouter.urllib.request.urlopen", fake_urlopen):
            result = self.model.generate(**kwargs)
        return result, captured

    def test_parses_tool_call_response(self):
        payload = {
            "model": "anthropic/claude-opus-4.8",
            "usage": {"total_tokens": 5},
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "calculator",
                                    "arguments": '{"expression": "(12 + 30) * 2"}',
                                },
                            }
                        ],
                    },
                }
            ],
        }
        result, captured = self._generate(
            payload,
            messages=[Message.user("What is (12 + 30) * 2?")],
            tools=[{"type": "function", "function": {"name": "calculator"}}],
        )
        self.assertEqual(captured["url"], "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(captured["body"]["model"], "anthropic/claude-opus-4.8")
        self.assertIn("tools", captured["body"])
        self.assertEqual(captured["body"]["tool_choice"], "auto")
        self.assertEqual(len(result.tool_calls), 1)
        self.assertEqual(result.tool_calls[0].name, "calculator")
        self.assertEqual(result.tool_calls[0].arguments, {"expression": "(12 + 30) * 2"})

    def test_parses_text_response(self):
        payload = {
            "model": "anthropic/claude-opus-4.8",
            "choices": [{"finish_reason": "stop", "message": {"content": "The answer is 84."}}],
        }
        result, _ = self._generate(payload, messages=[Message.user("hi")])
        self.assertEqual(result.text, "The answer is 84.")
        self.assertEqual(result.tool_calls, [])

    def test_authorization_header_present(self):
        payload = {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]}
        _, captured = self._generate(payload, messages=[Message.user("hi")])
        # urllib title-cases header keys.
        self.assertEqual(captured["headers"].get("Authorization"), "Bearer test-key")


if __name__ == "__main__":
    unittest.main()
