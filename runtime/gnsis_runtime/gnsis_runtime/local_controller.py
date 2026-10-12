"""Bounded Genesis 2.0 decisions over Host-offered action candidates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

MAX_CANDIDATES = 12
MAX_HISTORY = 12
MAX_PAYLOAD_TEXT = 16_000


@dataclass(frozen=True)
class Candidate:
    key: str
    kind: str
    description: str
    tool: str | None = None
    args: dict[str, Any] | None = None
    message: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class Selection:
    key: str | None
    confidence: float
    abstain: float = 0.0
    valid: bool = True


class LocalModelBackend(Protocol):
    def candidates(
        self,
        *,
        text: str,
        history: list[dict[str, Any]],
        tools: list[str],
        tool_schemas: list[dict[str, Any]],
    ) -> list[Candidate]: ...

    def select(
        self,
        *,
        text: str,
        history: list[dict[str, Any]],
        candidates: list[Candidate],
    ) -> Selection: ...


class LocalDecisionEngine:
    """Keeps model output inside the exact actions offered by the desktop Host."""

    def __init__(self, backend: LocalModelBackend):
        self.backend = backend

    def decide(self, payload: dict[str, Any]) -> dict[str, Any]:
        text = payload.get("text")
        tools = payload.get("tools")
        history = payload.get("history", [])
        tool_schemas = payload.get("tool_schemas", [])
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text) > MAX_PAYLOAD_TEXT
            or not isinstance(tools, list)
            or not all(isinstance(tool, str) and tool for tool in tools)
            or not isinstance(history, list)
            or len(history) > MAX_HISTORY
            or not isinstance(tool_schemas, list)
        ):
            raise ValueError("Invalid local controller request")

        offered = set(tools)
        schemas = [
            schema
            for schema in tool_schemas
            if isinstance(schema, dict) and schema.get("name") in offered
        ]
        proposed = self.backend.candidates(
            text=text.strip(),
            history=history,
            tools=tools,
            tool_schemas=schemas,
        )
        candidates = self._validated_candidates(proposed, offered)
        if not candidates:
            return {"kind": "fallback", "reason": "No safe local action was proposed."}

        selection = self.backend.select(
            text=text.strip(),
            history=history,
            candidates=candidates,
        )
        if (
            not selection.valid
            or selection.key is None
            or selection.abstain >= selection.confidence
        ):
            return {"kind": "fallback", "reason": "The local model abstained."}
        by_key = {candidate.key: candidate for candidate in candidates}
        chosen = by_key.get(selection.key)
        if chosen is None:
            return {"kind": "fallback", "reason": "The local model chose no offered action."}
        if chosen.kind == "tool":
            return {
                "kind": "tool",
                "tool": chosen.tool,
                "args": chosen.args,
                "confidence": selection.confidence,
            }
        if chosen.kind == "done":
            return {"kind": "done", **({"message": chosen.message} if chosen.message else {})}
        return {
            "kind": "fallback",
            "reason": chosen.reason or "The local model could not safely continue.",
        }

    @staticmethod
    def _validated_candidates(
        proposed: list[Candidate], offered: set[str]
    ) -> list[Candidate]:
        out: list[Candidate] = []
        keys: set[str] = set()
        for candidate in proposed[:MAX_CANDIDATES]:
            if (
                not candidate.key
                or candidate.key in keys
                or not candidate.description
                or len(candidate.description) > 1_000
            ):
                continue
            if candidate.kind == "tool":
                if candidate.tool not in offered or not isinstance(candidate.args, dict):
                    continue
            elif candidate.kind == "done":
                if candidate.message is not None and len(candidate.message) > 2_000:
                    continue
            elif candidate.kind == "fallback":
                if candidate.reason is not None and len(candidate.reason) > 2_000:
                    continue
            else:
                continue
            keys.add(candidate.key)
            out.append(candidate)
        return out
