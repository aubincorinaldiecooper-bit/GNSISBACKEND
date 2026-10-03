"""An external reasoning model as the planner over the Smaller GNSIS contract.

The planner sees only what the production planner surface returns — the task,
the legal action set, the viewport, the service decision for the current frame,
and the host's own report of what previous actions did. It never receives the
DOM, selectors, expected targets, or scoring labels, and it never executes
anything: it answers with one bounded action that the trusted host then
authorizes, executes, records, and verifies.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .browser_host import PlannerObservation

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

SYSTEM_PROMPT = """\
You are the planner in a controlled browser-automation test harness evaluating
the GNSIS visual service on a local sandbox page. You cannot see the screen
yourself: each step you receive the visual service's structured perception of
the current frame plus the host's report of what previous steps did, and you
choose the next bounded action for the harness to perform.

Reply with a single JSON object and nothing else:
- "action": one of the legal actions given to you.
- click and type need "target": {"x": <0..1>, "y": <0..1>} normalized to the
  viewport; type also needs "text".
- scroll needs "direction" ("up" or "down"); navigate needs an http(s) "url".
- Use "wait" only when another frame is genuinely likely to change the answer;
  it makes no progress.
- Use "done" only when the host's reported results show the task is already
  complete. Declaring done early is a failure.
- Prefer the service's proposed target when it has one; override it when the
  history shows it did not work.
Include a short "why" string explaining the choice.\
"""

_ACTION_KEYS = ("action", "target", "text", "url", "direction", "confidence")


@dataclass
class PlannerUsage:
    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_latency_ms: int = 0
    errors: list[str] = field(default_factory=list)


class OpenRouterPlanner:
    """Plans bounded host actions with an OpenAI-compatible chat model."""

    def __init__(
        self,
        model: str,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 120.0,
        max_tokens: int = 700,
    ) -> None:
        self.model = model
        self.usage = PlannerUsage()
        self._max_tokens = max_tokens
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/aubincorinaldiecooper-bit/gnsis",
                "X-Title": "GNSIS",
            },
        )

    def close(self) -> None:
        self._client.close()

    async def plan(self, observation: PlannerObservation) -> dict[str, Any]:
        prompt = _render_observation(observation)
        started = time.monotonic()
        try:
            response = self._client.post(
                "/chat/completions",
                json={
                    "model": self.model,
                    "max_tokens": self._max_tokens,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                },
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            latency_ms = int((time.monotonic() - started) * 1000)
            self.usage.requests += 1
            self.usage.total_latency_ms += latency_ms
            self.usage.errors.append(f"{type(exc).__name__}: {exc}")
            return _fallback(
                observation,
                {
                    "model": self.model,
                    "latency_ms": latency_ms,
                    "error": f"{type(exc).__name__}: {exc}",
                    "fallback": "service_decision",
                },
            )

        latency_ms = int((time.monotonic() - started) * 1000)
        usage = payload.get("usage") or {}
        self.usage.requests += 1
        self.usage.total_latency_ms += latency_ms
        self.usage.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.usage.completion_tokens += int(usage.get("completion_tokens") or 0)

        text = _first_content(payload)
        trace: dict[str, Any] = {
            "model": payload.get("model", self.model),
            "latency_ms": latency_ms,
            "usage": usage,
            "response_text": text,
        }
        decision = _parse_decision(text, observation.allowed_actions)
        if decision is None:
            trace["fallback"] = "service_decision"
            trace["error"] = "planner response was not a legal bounded action"
            return _fallback(observation, trace)
        trace["why"] = decision.pop("why", "")
        decision["trace"] = trace
        return decision


def _render_observation(observation: PlannerObservation) -> str:
    width, height = observation.viewport
    history = [
        {
            "step": entry.get("step"),
            "executed": {
                key: value
                for key, value in (entry.get("executed_decision") or {}).items()
                if key in _ACTION_KEYS
            },
            "host_success": entry.get("success"),
            "host_message": entry.get("message"),
        }
        for entry in observation.history
    ]
    return json.dumps(
        {
            "task": observation.task,
            "step": observation.step,
            "legal_actions": list(observation.allowed_actions),
            "viewport": {"width": width, "height": height},
            "visual_service_decision": observation.service_decision,
            "previous_steps": history,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _first_content(payload: dict[str, Any]) -> str:
    choices = payload.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def _parse_decision(
    text: str,
    allowed_actions: tuple[str, ...],
) -> dict[str, Any] | None:
    raw = _extract_object(text)
    if raw is None:
        return None
    action = raw.get("action")
    if not isinstance(action, str) or action not in allowed_actions:
        return None
    decision: dict[str, Any] = {"action": action}
    why = raw.get("why")
    decision["why"] = why if isinstance(why, str) else ""

    target = raw.get("target")
    if not isinstance(target, dict) and {"x", "y"} <= raw.keys():
        target = {"x": raw.get("x"), "y": raw.get("y")}
    if action in {"click", "type"}:
        if not isinstance(target, dict):
            return None
        try:
            x = float(target["x"])
            y = float(target["y"])
        except (KeyError, TypeError, ValueError):
            return None
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            return None
        decision["target"] = {"x": x, "y": y}
    if action == "type":
        text_value = raw.get("text")
        if not isinstance(text_value, str) or not text_value:
            return None
        decision["text"] = text_value
    if action == "scroll":
        direction = raw.get("direction")
        if direction not in {"up", "down"}:
            return None
        decision["direction"] = direction
    if action == "navigate":
        url = raw.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            return None
        decision["url"] = url
    return decision


def _extract_object(text: str) -> dict[str, Any] | None:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        _, _, candidate = candidate.partition("\n")
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(candidate[start : end + 1])
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _fallback(
    observation: PlannerObservation,
    trace: dict[str, Any],
) -> dict[str, Any]:
    """Fall back to the service's own decision so a planner fault is visible
    in the trace instead of aborting the run."""
    decision = {
        key: value
        for key, value in observation.service_decision.items()
        if key in _ACTION_KEYS
    }
    decision["trace"] = trace
    return decision
