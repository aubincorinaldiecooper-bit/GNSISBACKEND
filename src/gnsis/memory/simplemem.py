"""Omni-SimpleMem-backed long-term memory provider.

Client for the GNSIS SimpleMem sidecar (``tools/simplemem/sidecar.py``), the
hardened HTTP boundary around the upstream open-source ``simplemem`` package
ported from the production deployment that hardened it (PRs #106/#107/#109/
#114/#123/#126 there).

Design contract (per AGENTS.md):

* **Repo-scoped namespaces** — ``repo`` remains the primary namespace; one
  sidecar namespace per repo means one project's memory never leaks into
  another's.
* **Approval-gated writes** — ``write`` refuses records whose ``approved``
  flag is ``False``; durable memory only stores validated outcomes.
* **Explicit memory types** — each record carries one of the locked types
  (``episode``, ``visual_event``, ``audio_event``, ``fact``, ``decision``,
  ``preference``, ``task_result``, ``approved_code_intelligence``) and its
  provenance (``memory_id``, ``source_job_id``, workspace/repository ids, and
  any source-time/session references).
* **Fail loudly** — when configured but unreachable, calls raise
  :class:`SimpleMemUnavailable`; production readiness should surface that
  rather than silently running memoryless.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from .base import MemoryProvider, MemoryRecord

# One structured line per sidecar call; cheap enough to always emit and the
# primary real-time observability hook for this integration.
TELEMETRY = logging.getLogger("gnsis.memory.telemetry")

# Locked memory types (AGENTS.md, decision 3).
MEMORY_TYPES = frozenset(
    {
        "episode",
        "visual_event",
        "audio_event",
        "fact",
        "decision",
        "preference",
        "task_result",
        "approved_code_intelligence",
    }
)

_KIND_TO_TYPE = {
    "decision": "decision",
    "preference": "preference",
    "accepted_change": "task_result",
    "rejected_change": "fact",
    "rejection_lesson": "fact",
    "convention": "fact",
    "task_result": "task_result",
    "episode": "episode",
    "visual_event": "visual_event",
    "audio_event": "audio_event",
    "approved_code_intelligence": "approved_code_intelligence",
}


class SimpleMemUnavailable(RuntimeError):
    """The configured memory service is unreachable or misbehaving."""


def memory_type_for_kind(kind: str) -> str:
    return _KIND_TO_TYPE.get(kind, kind if kind in MEMORY_TYPES else "fact")


def namespace_for_repo(repo: str) -> str:
    """The sidecar namespace for one repo-scoped memory store."""
    return f"repo:{repo}"


def _hit_to_record(hit: Dict[str, Any], repo: str) -> MemoryRecord:
    metadata = hit.get("metadata")
    provenance = hit.get("provenance")
    merged: Dict[str, Any] = {}
    if isinstance(provenance, dict):
        merged.update(provenance)
    if isinstance(metadata, dict):
        merged.update(metadata)
    merged["mau_id"] = hit.get("mauId") or hit.get("ref")
    merged["score"] = hit.get("score")
    merged["memory_type"] = hit.get("type")
    return MemoryRecord(
        repo=repo,
        content=str(hit.get("summary") or hit.get("text") or ""),
        kind=str(hit.get("type") or "fact"),
        metadata=merged,
        approved=True,
        memory_id=str(provenance.get("memory_id"))
        if isinstance(provenance, dict) and provenance.get("memory_id")
        else None,
        source_job_id=str(provenance.get("source_job_id"))
        if isinstance(provenance, dict) and provenance.get("source_job_id")
        else None,
        workspace_id=str(provenance.get("workspace_id"))
        if isinstance(provenance, dict) and provenance.get("workspace_id")
        else None,
        repository_id=str(provenance.get("repository_id"))
        if isinstance(provenance, dict) and provenance.get("repository_id")
        else None,
    )


class SimpleMemProvider(MemoryProvider):
    """Durable, repo-scoped memory served by the GNSIS Omni-SimpleMem sidecar.

    Unlike the Postgres provider (the audit/source-of-truth for *approved
    coding intelligence*), this is the general multimodal recall surface:
    episodic and semantic memories across sessions, retrievable by the live
    runtime and by background workers through the same store.
    """

    name = "simplemem"

    def __init__(
        self,
        url: Optional[str] = None,
        *,
        token: Optional[str] = None,
        timeout_s: float = 30.0,
        max_response_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        import os

        url = (url or "").strip()
        if not url:
            url = os.environ.get("GNSIS_SIMPLEMEM_URL", "").strip()
        token = (token or "").strip() or os.environ.get(
            "GNSIS_SIMPLEMEM_INTERNAL_TOKEN", ""
        ).strip()
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(
                "SimpleMem provider requires an absolute http(s) URL "
                "(GNSIS_SIMPLEMEM_URL) when memory_backend='simplemem'"
            )
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError("SimpleMem URL must not contain credentials or a fragment")
        if not token:
            raise ValueError(
                "SimpleMem provider requires an internal token "
                "(GNSIS_SIMPLEMEM_INTERNAL_TOKEN, min 32 chars) when memory_backend='simplemem'"
            )
        if len(token) < 32:
            raise ValueError("SimpleMem internal token must be at least 32 characters")
        if timeout_s <= 0:
            raise ValueError("SimpleMem timeout must be positive")
        self.url = url.rstrip("/")
        self.token = token
        self.timeout_s = timeout_s
        self.max_response_bytes = max_response_bytes

    # -- HTTP plumbing -------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Optional[Dict[str, Any]] = None,
        timeout_s: Optional[float] = None,
    ) -> Dict[str, Any]:
        data = None
        headers = {
            "Accept": "application/json",
            "X-GNSIS-SimpleMem-Token": self.token,
        }
        if body is not None:
            data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self.url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        start = time.monotonic()
        try:
            with urlopen(request, timeout=timeout_s or self.timeout_s) as response:
                raw = response.read(self.max_response_bytes + 1)
        except HTTPError as exc:
            detail = ""
            try:
                detail = exc.read(2048).decode("utf-8", "replace")[:300]
            except Exception:
                pass
            TELEMETRY.warning(
                "simplemem.request op=%s_%s latency_ms=%.1f status=http_%s",
                method,
                path.split("/")[-1].split("?")[0] or "root",
                (time.monotonic() - start) * 1000,
                exc.code,
            )
            raise SimpleMemUnavailable(
                f"SimpleMem {method} {path} returned HTTP {exc.code}: {detail}"
            ) from exc
        except URLError as exc:
            TELEMETRY.warning(
                "simplemem.request op=%s_%s latency_ms=%.1f status=unreachable",
                method,
                path.split("/")[-1].split("?")[0] or "root",
                (time.monotonic() - start) * 1000,
            )
            raise SimpleMemUnavailable(
                f"SimpleMem {method} {path} unreachable: {exc.reason}"
            ) from exc
        if len(raw) > self.max_response_bytes:
            raise SimpleMemUnavailable("SimpleMem response exceeds the byte limit")
        TELEMETRY.info(
            "simplemem.request op=%s_%s latency_ms=%.1f status=ok",
            method,
            path.split("/")[-1].split("?")[0] or "root",
            (time.monotonic() - start) * 1000,
        )
        try:
            decoded = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SimpleMemUnavailable("SimpleMem response is not valid JSON") from exc
        if not isinstance(decoded, dict):
            raise SimpleMemUnavailable("SimpleMem response must be a JSON object")
        return decoded

    def _ns(self, repo: str) -> str:
        if not isinstance(repo, str) or not repo.strip():
            raise ValueError("repo must be a non-empty string")
        return quote(namespace_for_repo(repo.strip()), safe=":_-.")

    # -- MemoryProvider contract ---------------------------------------------

    def write(self, record: MemoryRecord) -> Optional[MemoryRecord]:
        if not record.approved:
            return None  # approval-gated: only validated outcomes persist
        provenance = {
            "memory_id": record.memory_id,
            "source_job_id": record.source_job_id,
            "workspace_id": record.workspace_id,
            "repository_id": record.repository_id,
            "repo": record.repo,
            "kind": record.kind,
            "created_at": record.created_at,
            "metadata": record.metadata,
        }
        provenance = {key: value for key, value in provenance.items() if value is not None and value != {}}
        reply = self._request(
            "PUT",
            f"/namespaces/{self._ns(record.repo)}/text",
            body={
                "text": record.content,
                "type": memory_type_for_kind(record.kind),
                "tags": [f"kind:{record.kind}"] if record.kind else None,
                "provenance": provenance,
            },
        )
        mau_id = reply.get("mauId")
        if isinstance(mau_id, str) and mau_id:
            record.metadata = {**record.metadata, "mau_id": mau_id}
        return record

    def write_episode(
        self,
        repo: str,
        *,
        text: str,
        memory_type: str = "episode",
        provenance: Optional[Dict[str, Any]] = None,
        session_id: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Write an episodic/semantic record directly (not approval-gated).

        Episodes are observations, not validated outcomes — the approval gate
        belongs to the authoritative CodeMemory path, not to perception
        records. Provenance must carry source-time and source references.
        """
        if memory_type not in MEMORY_TYPES:
            raise ValueError(f"unknown memory type {memory_type!r}")
        return self._request(
            "PUT",
            f"/namespaces/{self._ns(repo)}/text",
            body={
                "text": text,
                "type": memory_type,
                "session_id": session_id,
                "tags": tags,
                "provenance": provenance or {},
            },
        )

    def search(
        self,
        repo: str,
        query: str,
        limit: int = 5,
        *,
        types: Optional[List[str]] = None,
    ) -> List[MemoryRecord]:
        reply = self._request(
            "POST",
            f"/namespaces/{self._ns(repo)}/query",
            body={"query": query, "top_k": limit, "types": types},
        )
        items = reply.get("items") or reply.get("results") or []
        return [_hit_to_record(hit, repo) for hit in items if isinstance(hit, dict)]

    def recent(self, repo: str, limit: int = 20) -> List[MemoryRecord]:
        reply = self._request(
            "GET",
            f"/namespaces/{self._ns(repo)}/recent?limit={int(limit)}",
        )
        items = reply.get("items") or reply.get("results") or []
        return [_hit_to_record(hit, repo) for hit in items if isinstance(hit, dict)]

    # -- Health ---------------------------------------------------------------

    def health(self) -> Dict[str, Any]:
        """Unauthenticated liveness/config snapshot."""
        return self._request("GET", "/health")

    def ready(self) -> Dict[str, Any]:
        """Authenticated readiness; raises when the sidecar is not serving."""
        return self._request("GET", "/ready")

    def assert_ready(self) -> None:
        """Fail loudly when required memory is configured but unavailable."""
        self.ready()
