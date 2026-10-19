"""Bounded GNSIS 0.5 decisions over Host-offered action candidates."""

from __future__ import annotations

import base64
import binascii
import os
import tempfile
from dataclasses import dataclass
from typing import Any, Protocol

MAX_CANDIDATES = 12
MAX_HISTORY = 12
MAX_PAYLOAD_TEXT = 16_000
MAX_STATE_IMAGE_BYTES = 8 * 1024 * 1024


def _state_image_path(payload: dict[str, Any]) -> str | None:
    """Decode an optional base64 screen still to a temp file for the backend.

    The desktop attaches the latest still from its already-active screen share
    (never a fresh screenshot). Bad or oversized input falls back to the
    backend's default state image rather than failing the request.
    """
    encoded = payload.get("state_image")
    if not isinstance(encoded, str) or not encoded.strip():
        return None
    try:
        blob = base64.b64decode(encoded.strip(), validate=True)
    except (binascii.Error, ValueError):
        return None
    if not blob or len(blob) > MAX_STATE_IMAGE_BYTES:
        return None
    if blob[:3] == b"\xff\xd8\xff":
        suffix = ".jpg"
    elif blob[:8] == b"\x89PNG\r\n\x1a\n":
        suffix = ".png"
    else:
        return None
    fd, path = tempfile.mkstemp(prefix="gnsis-state-", suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(blob)
    except OSError:
        os.unlink(path)
        return None
    return path


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
        state_image: str | None = None,
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

        state_path = _state_image_path(payload)
        try:
            selection = self.backend.select(
                text=text.strip(),
                history=history,
                candidates=candidates,
                state_image=state_path,
            )
        finally:
            if state_path is not None:
                try:
                    os.unlink(state_path)
                except OSError:
                    pass
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
