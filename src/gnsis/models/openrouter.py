"""OpenRouter (and any OpenAI-compatible) chat-completions backend.

Implemented against the standard library only — no SDK dependency — so the
runtime installs and runs with nothing extra. Point ``base_url`` at any
OpenAI-compatible endpoint to use a different gateway.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from .base import BaseModel, Message, ModelResponse, ToolCall

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "anthropic/claude-opus-4.8"


def _message_to_payload(message: Message) -> Dict[str, Any]:
    if message.role == "tool":
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id,
            "content": message.content,
        }
    if message.role == "assistant" and message.tool_calls:
        return {
            "role": "assistant",
            "content": message.content or None,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments),
                    },
                }
                for call in message.tool_calls
            ],
        }
    return {"role": message.role, "content": message.content}


class OpenRouterModel(BaseModel):
    provider = "openrouter"

    def __init__(
        self,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        max_tokens: int = 1024,
        temperature: Optional[float] = None,
        timeout: float = 60.0,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self.model = model or os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL)
        self.api_key = (
            api_key
            or os.environ.get("OPENROUTER_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
        )
        self.base_url = (
            base_url or os.environ.get("OPENROUTER_BASE_URL", DEFAULT_BASE_URL)
        ).rstrip("/")
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout
        self.extra_headers = extra_headers or {}

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            # Optional OpenRouter attribution headers (harmless elsewhere).
            "HTTP-Referer": "https://github.com/aubincorinaldiecooper-bit/gnsis",
            "X-Title": "GNSIS",
        }
        headers.update(self.extra_headers)
        return headers

    def generate(
        self,
        messages: List[Message],
        tools: Optional[List[dict]] = None,
        **kwargs: Any,
    ) -> ModelResponse:
        if not self.api_key:
            raise RuntimeError(
                "No API key found. Set OPENROUTER_API_KEY (or pass api_key=...). "
                "For an offline run, use the 'mock' provider instead."
            )

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [_message_to_payload(m) for m in messages],
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
        }
        temperature = kwargs.get("temperature", self.temperature)
        if temperature is not None:
            payload["temperature"] = temperature
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = kwargs.get("tool_choice", "auto")

        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:  # surface the API's error body
            detail = exc.read().decode("utf-8", "replace")
            raise RuntimeError(f"OpenRouter HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"OpenRouter request failed: {exc.reason}") from exc

        return self._parse(data)

    def _parse(self, data: Dict[str, Any]) -> ModelResponse:
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"OpenRouter returned no choices: {data}")
        choice = choices[0]
        message = choice.get("message", {})
        text = message.get("content") or ""
        tool_calls = []
        for raw_call in message.get("tool_calls") or []:
            function = raw_call.get("function", {})
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except json.JSONDecodeError:
                arguments = {}
            tool_calls.append(
                ToolCall(
                    id=raw_call.get("id", ""),
                    name=function.get("name", ""),
                    arguments=arguments,
                )
            )
        return ModelResponse(
            text=text,
            tool_calls=tool_calls,
            finish_reason=choice.get("finish_reason", "stop"),
            usage=data.get("usage", {}),
            raw=data,
            model=data.get("model", self.model),
        )
