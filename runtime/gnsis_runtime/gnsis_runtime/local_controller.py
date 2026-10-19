"""Bounded GNSIS 0.5 decisions over Host-offered action candidates."""

from __future__ import annotations

import base64
import binascii
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from typing import Any, Callable, Protocol

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


class DecisionCancelled(Exception):
    """The HTTP client went away mid-decide; any result would be discarded."""


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
    """Keeps model output inside the exact actions offered by the desktop Host.

    The model backend is not thread-safe, so model work is serialized under a
    lock; requests queue instead of interleaving inside a single model call.
    `cancelled` is a cooperative check between model stages so a client that
    hung up does not keep the CPU grinding on a discarded answer.
    """

    def __init__(self, backend: LocalModelBackend):
        self.backend = backend
        self._lock = threading.Lock()

    def decide(
        self,
        payload: dict[str, Any],
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
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
        with self._lock:
            if cancelled is not None and cancelled():
                raise DecisionCancelled()
            proposed = self.backend.candidates(
                text=text.strip(),
                history=history,
                tools=tools,
                tool_schemas=schemas,
            )
            candidates = self._validated_candidates(proposed, offered)
            if not candidates:
                return {
                    "kind": "fallback",
                    "reason": "No safe local action was proposed.",
                }

            if cancelled is not None and cancelled():
                raise DecisionCancelled()
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
            return {
                "kind": "fallback",
                "reason": "The local model chose no offered action.",
            }
        if chosen.kind == "tool":
            return {
                "kind": "tool",
                "tool": chosen.tool,
                "args": chosen.args,
                "confidence": selection.confidence,
            }
        if chosen.kind == "done":
            return {
                "kind": "done",
                **({"message": chosen.message} if chosen.message else {}),
            }
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
                if candidate.tool not in offered or not isinstance(
                    candidate.args, dict
                ):
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


_OPEN_TARGET = re.compile(
    r"^(?:please\s*,?\s+)?(?:open|launch|start)\s+(.+?)(?:\s*,?\s+please)?[.!?]*$",
    re.IGNORECASE,
)
_CLICK_LABEL = re.compile(
    r"^(?:click|tap|press\s+(?:the|on))\s+(?:the\s+)?(.+?)[.!?]*$", re.IGNORECASE
)
_TYPE_TEXT = re.compile(r"^type\s+(.+?)(?:\s+into\s+.+)?[.!?]*$", re.IGNORECASE)
_KEYS = re.compile(r"^(?:press|hit)\s+(?!the\b|on\b)(.+?)[.!?]*$", re.IGNORECASE)
_SCROLL = re.compile(r"\bscroll\s+(up|down|left|right)\b", re.IGNORECASE)
_GO_URL = re.compile(
    r"^(?:go\s+to|visit|navigate\s+to|browse\s+to)\s+(\S+?)[.!?]*$", re.IGNORECASE
)
_NEW_FOLDER = re.compile(
    r"(?:create|make|new)\s+(?:a\s+)?(?:new\s+)?folder\s+(?:called|named\s+)?\s*(.+?)[.!?]*$",
    re.IGNORECASE,
)
_FIND = re.compile(
    r"^(?:find|search\s+for|look\s+for|locate)\s+(?:the\s+|a\s+|an\s+)?(.+?)[.!?]*$",
    re.IGNORECASE,
)
_CLIPBOARD_READ = re.compile(
    r"\bclipboard\b.*\b(?:what|show|read|see)\b|\b(?:what|show|read|see)\b.*\bclipboard\b",
    re.IGNORECASE,
)
_COPY = re.compile(
    r"^(?:copy|save\s+to\s+(?:my\s+)?clipboard)\s+(.+?)[.!?]*$", re.IGNORECASE
)


def _text_arg(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text.strip())
    if match is None:
        return None
    value = match.group(1).strip().rstrip(".,!?").strip()
    return value or None


def _tool_candidate(
    name: str, text: str, description: str
) -> tuple[dict[str, Any] | None, str]:
    """Ground a schema-offered tool in the trusted request words, when plain.

    Returns (args, candidate description). Arguments are only extracted where
    the words map onto the schema unambiguously; a None args means the words
    did not plainly name this tool's action.
    """
    if name == "open":
        target = _text_arg(_OPEN_TARGET, text)
        if target and len(target) <= 512:
            return {"target": target}, f"Open '{target}' on the person's Mac."
        return None, description
    elif name == "input":
        label = _text_arg(_CLICK_LABEL, text)
        if label and len(label) <= 128:
            return {
                "action": "click",
                "label": label,
            }, f"Click '{label}' on the screen."
        typed = _text_arg(_TYPE_TEXT, text)
        if typed and len(typed) <= 1000:
            return {"action": "type", "text": typed}, f"Type the words '{typed[:80]}'."
        keys = _text_arg(_KEYS, text)
        if keys and len(keys) <= 64:
            return {"action": "keys", "keys": keys}, f"Press {keys}."
        scroll = _SCROLL.search(text)
        if scroll:
            direction = scroll.group(1).lower()
            return {"action": "scroll", "direction": direction}, f"Scroll {direction}."
    elif name == "browser":
        url = _text_arg(_GO_URL, text)
        if url and len(url) <= 2048:
            return {"action": "go", "url": url}, f"Open {url} in the person's browser."
    elif name == "files":
        folder = _text_arg(_NEW_FOLDER, text)
        if folder and len(folder) <= 255:
            return {
                "action": "new_folder",
                "name": folder,
            }, f"Create a folder named '{folder}'."
        query = _text_arg(_FIND, text)
        if query and len(query) <= 128:
            return {
                "action": "find",
                "query": query,
            }, f"Find '{query}' in the open Finder."
    elif name == "clipboard":
        if _CLIPBOARD_READ.search(text):
            return {"action": "read"}, "Read what is on the person's clipboard."
        copied = _text_arg(_COPY, text)
        if copied and len(copied) <= 20_000:
            return {
                "action": "write",
                "text": copied,
            }, f"Copy '{copied[:80]}' to the clipboard."
    return None, description


def schema_candidates(
    *,
    text: str,
    tools: list[str],
    tool_schemas: list[dict[str, Any]],
) -> list[Candidate]:
    """Deterministic action candidates built from the offered tool schemas.

    The judge (`system_one`) still makes the decision with the screen still;
    this only avoids a minutes-long free-form generation to enumerate options
    the Host already described exactly. Arguments are grounded in the trusted
    request text where a plain pattern exists, matching the same intent the
    desktop's deterministic `localCommand` matcher uses.

    Only tools the words plainly name go on the ballot — a longer ballot
    splits the judge's confidence so thin that even a committed pick falls
    under the Host's confidence gate. When nothing matches, every offered
    tool is on the ballot generic, which is the honest low-confidence case.
    """
    offered = set(tools)
    grounded: list[Candidate] = []
    generic: list[Candidate] = []
    for schema in tool_schemas:
        if not isinstance(schema, dict):
            continue
        name = schema.get("name")
        if not isinstance(name, str) or name not in offered:
            continue
        description = schema.get("description")
        blurb = description if isinstance(description, str) else name
        args, blurb = _tool_candidate(name, text, blurb)
        bucket = grounded if args is not None else generic
        bucket.append(
            Candidate(
                key="",
                kind="tool",
                tool=name,
                args=args if args is not None else {},
                description=blurb[:1_000],
            )
        )
    out = grounded if grounded else generic
    for index, candidate in enumerate(out):
        out[index] = Candidate(
            key=f"candidate-{index + 1}",
            kind=candidate.kind,
            description=candidate.description,
            tool=candidate.tool,
            args=candidate.args,
            message=candidate.message,
            reason=candidate.reason,
        )
    out.append(
        Candidate(
            key=f"candidate-{len(out) + 1}",
            kind="done",
            description="The request is fully handled; finish in plain words.",
        )
    )
    out.append(
        Candidate(
            key=f"candidate-{len(out) + 1}",
            kind="fallback",
            description="Decline: the request cannot be handled safely on this Mac.",
        )
    )
    return out
