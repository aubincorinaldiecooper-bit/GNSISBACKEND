"""Tools the connected Host runs on the person's own machine.

Two halves have to agree before the model may be shown one of these tools:

- the deployment owns the schema, which is the text the model reads. It lives
  in one reviewed catalog file (``duplex.host_tools_path``) with a version;
- the Host owns the implementation, and says when it connects which catalog
  version it speaks and which of its tools it can run right now.

A session is shown the intersection, and only when the versions match. The
model is never shown a tool the connected Host cannot execute, and a Host
adapter the deployment has not reviewed never becomes model-visible. A web
page, which offers nothing, gets the base tool set exactly as before.

The Host's offer travels in the ``/ws/duplex`` query string because the
runtime builds the model session as soon as the socket opens, before any
control message could arrive. Only names cross the wire; schema text never
comes from the client, so a client cannot write into the model's prompt.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

HOST_TOOLS_PARAM = "host_tools"
HOST_TOOLS_VERSION_PARAM = "host_tools_version"

# Enough for a compact surface; a longer offer is a malformed client, not a
# bigger toolbox.
MAX_OFFERED_TOOLS = 16
_TOOL_NAME = re.compile(r"[a-z][a-z0-9_]{0,31}")


@dataclass(frozen=True)
class HostToolCatalog:
    """The deployment's reviewed set of Host-executed tool schemas."""

    version: str
    schemas: tuple[dict[str, Any], ...]

    def names(self) -> tuple[str, ...]:
        return tuple(str(schema["name"]) for schema in self.schemas)

    def select(self, names: Sequence[str]) -> tuple[dict[str, Any], ...]:
        """The catalog's schemas for ``names``, in catalog order."""

        wanted = set(names)
        return tuple(schema for schema in self.schemas if schema["name"] in wanted)


@dataclass(frozen=True)
class HostToolNegotiation:
    """What one session agreed with its Host. Recorded on ready and on /health."""

    version: str | None
    offered: tuple[str, ...]
    accepted: tuple[str, ...]
    rejected: tuple[str, ...]
    reason: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "offered": list(self.offered),
            "accepted": list(self.accepted),
            "rejected": list(self.rejected),
            "reason": self.reason,
        }


NO_HOST_TOOLS = HostToolNegotiation(
    version=None, offered=(), accepted=(), rejected=(), reason=""
)


def load_host_tool_catalog(path: str | Path | None) -> HostToolCatalog | None:
    """Read ``{"version": "...", "tools": [...]}``; None when no path is set."""

    if not path:
        return None
    from mcpmft.tool_protocol import normalize_tool_schema

    document = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(document, Mapping):
        raise ValueError("duplex.host_tools_path must hold a JSON object")
    version = document.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("duplex.host_tools_path needs a non-empty version")
    tools = document.get("tools")
    if not isinstance(tools, list) or not tools:
        raise ValueError("duplex.host_tools_path needs a non-empty tools list")
    schemas = tuple(normalize_tool_schema(tool) for tool in tools)
    names = [schema["name"] for schema in schemas]
    if len(names) != len(set(names)):
        raise ValueError("duplex.host_tools_path repeats a tool name")
    for name in names:
        if not _TOOL_NAME.fullmatch(name):
            raise ValueError(f"host tool name {name!r} is not a plain lowercase name")
    return HostToolCatalog(version=version.strip(), schemas=schemas)


def parse_offer(raw: str | None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split the Host's comma-separated offer into (well-formed, malformed)."""

    if not raw:
        return (), ()
    names: list[str] = []
    malformed: list[str] = []
    for part in raw.split(","):
        name = part.strip()
        if not name:
            continue
        if not _TOOL_NAME.fullmatch(name) or len(names) >= MAX_OFFERED_TOOLS:
            malformed.append(name[:32])
            continue
        if name not in names:
            names.append(name)
    return tuple(names), tuple(malformed)


def negotiate(
    catalog: HostToolCatalog | None,
    offered_raw: str | None,
    offered_version: str | None,
) -> HostToolNegotiation:
    """Decide which offered tools this session's model may be shown.

    Fails closed: a version mismatch exposes none of them, because a schema
    the Host was not built against could describe arguments it reads
    differently.
    """

    offered, malformed = parse_offer(offered_raw)
    if not offered and not malformed:
        return NO_HOST_TOOLS
    version = (offered_version or "").strip() or None
    if catalog is None:
        return HostToolNegotiation(
            version=version,
            offered=offered,
            accepted=(),
            rejected=(*offered, *malformed),
            reason="no_catalog",
        )
    if version != catalog.version:
        return HostToolNegotiation(
            version=version,
            offered=offered,
            accepted=(),
            rejected=(*offered, *malformed),
            reason="version_mismatch",
        )
    known = set(catalog.names())
    accepted = tuple(name for name in catalog.names() if name in offered)
    rejected = (*(name for name in offered if name not in known), *malformed)
    return HostToolNegotiation(
        version=version,
        offered=offered,
        accepted=accepted,
        rejected=tuple(rejected),
        reason="" if not rejected else "not_in_catalog",
    )
