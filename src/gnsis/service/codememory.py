"""CodeMemory — the application layer over repo-scoped agent memory.

This is where GNSIS turns *approved* run outcomes into durable, structured
knowledge about how a specific repository is built, and hands a bounded, scoped,
deterministic slice of that knowledge back to a future run. The underlying model
is rented and can't be out-coded; this compounding, per-repo memory is the edge.

Design invariants (all enforced here, verifiable by tests):

* **Tenant-strict scoping.** Retrieval is filtered to one ``repo`` and, when a
  ``workspace_id`` is given, to that workspace's rows (plus legacy rows with no
  workspace). One workspace's memory is never surfaced to another.
* **Approval-gated writes only.** Every write goes through
  :class:`PostgresMemoryProvider.write`, which persists only ``approved`` rows.
  Callers here are the *accepted-change* and *rejection-lesson* recorders — both
  outcomes a human has already acted on. Unreviewed agent claims never land here.
* **Bounded + deterministic retrieval.** A run sees at most ``limit`` items,
  chosen by a stable, explainable ranking, so the same task against the same DB
  state always yields the same context (retry-safe, auditable).
* **A typed taxonomy.** Every memory declares what *kind* of knowledge it is, so
  standing constraints (conventions, security/testing rules, architectural
  decisions) are surfaced broadly while episodic memories (accepted changes,
  rejection lessons) surface when they match the task.

This layer holds no SQL of its own: all persistence lives in
:class:`PostgresMemoryProvider`. It adds only the taxonomy, the ranking, the
selection reasons, and the provenance plumbing.
"""

from __future__ import annotations

import hashlib

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from ..memory.base import MemoryRecord
from .repository import PostgresMemoryProvider


class MemoryKind:
    """The knowledge taxonomy CodeMemory records and retrieves.

    Split into two behavioural tiers used by the ranker:

    * *standing* — broadly relevant to any change in the repo, surfaced even
      when the task text doesn't mention them (a security rule still applies).
    * *episodic* — tied to a specific past change, surfaced only when the task
      overlaps them, so old specifics don't crowd out the current task.
    """

    CONVENTION = "convention"
    ARCHITECTURAL_DECISION = "architectural_decision"
    ACCEPTED_CHANGE = "accepted_change"
    REJECTION_LESSON = "rejection_lesson"
    DEPENDENCY_PREFERENCE = "dependency_preference"
    SECURITY_CONSTRAINT = "security_constraint"
    TESTING_CONSTRAINT = "testing_constraint"

    STANDING = frozenset(
        {
            CONVENTION,
            ARCHITECTURAL_DECISION,
            DEPENDENCY_PREFERENCE,
            SECURITY_CONSTRAINT,
            TESTING_CONSTRAINT,
        }
    )
    EPISODIC = frozenset({ACCEPTED_CHANGE, REJECTION_LESSON})
    ALL = STANDING | EPISODIC


@dataclass(frozen=True)
class MemoryItem:
    """One retrieved memory, with the reason it was selected."""

    memory_id: str
    kind: str
    content: str
    repository_id: Optional[str]
    workspace_id: Optional[str]
    source_job_id: Optional[str]
    created_at: str
    selection_reason: str = ""
    metadata: Dict[str, object] = field(default_factory=dict)

    def to_public_dict(self) -> dict:
        """Shape handed to a run (and echoed in its receipt). No internal ids."""
        return {
            "memory_id": self.memory_id,
            "kind": self.kind,
            "content": self.content,
            "selection_reason": self.selection_reason,
        }


@dataclass(frozen=True)
class MemorySelection:
    """The bounded set of memories chosen for one task."""

    items: List[MemoryItem]
    total_available: int
    truncated: bool
    query: str

    @property
    def memory_ids(self) -> List[str]:
        return [item.memory_id for item in self.items]

    def to_public_dicts(self) -> List[dict]:
        return [item.to_public_dict() for item in self.items]


# A tiny, deliberately conservative stopword set so short instructions like
# "fix the login bug" rank on "login"/"bug", not on "the"/"fix".
_STOPWORDS = frozenset(
    {
        "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "with",
        "is", "are", "be", "this", "that", "it", "as", "at", "by", "from",
        "add", "fix", "update", "make", "change", "please", "should", "would",
    }
)


def _terms(text: str) -> List[str]:
    out: List[str] = []
    for raw in (text or "").lower().split():
        tok = "".join(ch for ch in raw if ch.isalnum())
        if len(tok) >= 3 and tok not in _STOPWORDS:
            out.append(tok)
    # De-duplicate while preserving order.
    seen: set = set()
    uniq: List[str] = []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def reviewed_item_key(*, kind: str, content: str, item_key: Optional[str] = None) -> str:
    """Stable idempotency key for one explicit reviewed intelligence item."""
    explicit = (item_key or "").strip()
    if explicit:
        return explicit[:128]
    digest = hashlib.sha256(
        f"{kind}\0{(content or '').strip()}".encode("utf-8")
    ).hexdigest()
    return digest[:64]


def reviewed_content_hash(content: str) -> str:
    return hashlib.sha256((content or "").strip().encode("utf-8")).hexdigest()


class CodeMemory:
    """Application service over :class:`PostgresMemoryProvider`.

    Construct with a provider (defaults to the Postgres backend). All scoping and
    approval enforcement lives in the provider; the ranking + taxonomy live here.
    """

    #: hard ceiling on how many candidate rows the ranker will consider
    CANDIDATE_LIMIT = 200
    #: default number of items handed to a run
    DEFAULT_LIMIT = 6

    def __init__(self, provider: Optional[PostgresMemoryProvider] = None) -> None:
        self._provider = provider or PostgresMemoryProvider()

    # -- retrieval --------------------------------------------------------
    def retrieve_for_task(
        self,
        *,
        repo: str,
        instruction: str,
        workspace_id: Optional[str] = None,
        repository_id: Optional[str] = None,
        limit: int = DEFAULT_LIMIT,
    ) -> MemorySelection:
        """Return a bounded, scoped, deterministic memory slice for a task.

        Ranking (all deterministic for a fixed DB state):

        1. episodic memories score only on task-term overlap (0 → excluded);
        2. standing memories get a base score of 1 (always eligible) plus term
           overlap, so a matching convention outranks a non-matching one but a
           relevant accepted-change can still surface above generic standing rows;
        3. ties break by recency (the provider returns newest-first, and the sort
           is stable), then by ``memory_id`` for total order.
        """
        limit = max(0, int(limit))
        candidates = self._provider.recent_scoped(
            repo=repo,
            workspace_id=workspace_id,
            repository_id=repository_id,
            limit=self.CANDIDATE_LIMIT,
        )
        terms = _terms(instruction)

        scored: List[tuple] = []
        for rec in candidates:
            kind = rec.kind or "note"
            haystack = rec.content.lower()
            matched = [t for t in terms if t in haystack]
            standing = kind in MemoryKind.STANDING
            score = (2 * len(matched)) + (1 if standing else 0)
            if score <= 0:
                continue  # a non-matching episodic memory is not relevant here
            scored.append((score, matched, standing, rec))

        # Stable sort by score desc; candidates already arrive newest-first, so
        # equal scores keep recency order (Python's sort is stable).
        scored.sort(key=lambda t: t[0], reverse=True)

        total_available = len(scored)
        chosen = scored[:limit] if limit else []
        items = [
            self._to_item(rec, self._reason(kind_matched, standing))
            for _, kind_matched, standing, rec in chosen
        ]
        return MemorySelection(
            items=items,
            total_available=total_available,
            truncated=total_available > len(items),
            query=instruction,
        )

    def get_records_by_ids(
        self,
        *,
        memory_ids: Sequence[str],
        workspace_id: Optional[str] = None,
        repository_id: Optional[str] = None,
        repo: Optional[str] = None,
    ) -> List[MemoryItem]:
        """Reconstruct pinned memories by their handles (retry/audit path).

        Applies the same tenant scoping as retrieval, so a pinned id belonging to
        another workspace resolves to nothing. Order follows ``memory_ids``.
        """
        records = self._provider.by_memory_ids(
            memory_ids=list(memory_ids),
            workspace_id=workspace_id,
            repository_id=repository_id,
            repo=repo,
        )
        return [self._to_item(rec, "pinned") for rec in records]

    # -- writes (approval-gated via the provider) -------------------------
    def record_accepted_change(
        self,
        *,
        repo: str,
        source_job_id: str,
        content: str,
        workspace_id: Optional[str] = None,
        repository_id: Optional[str] = None,
        kind: str = MemoryKind.ACCEPTED_CHANGE,
        metadata: Optional[Dict[str, object]] = None,
        item_key: Optional[str] = None,
    ) -> Optional[MemoryItem]:
        """Persist the lesson of a change a human approved.

        ``kind`` defaults to ``accepted_change`` but may be any taxonomy kind, so
        an approved run that established (say) a convention can be recorded as one.
        Empty content is ignored (nothing to learn).
        """
        return self._write(
            repo=repo,
            content=content,
            kind=kind if kind in MemoryKind.ALL else MemoryKind.ACCEPTED_CHANGE,
            workspace_id=workspace_id,
            repository_id=repository_id,
            source_job_id=source_job_id,
            metadata=metadata,
        )

    def record_rejection_lesson(
        self,
        *,
        repo: str,
        source_job_id: str,
        content: str,
        workspace_id: Optional[str] = None,
        repository_id: Optional[str] = None,
        metadata: Optional[Dict[str, object]] = None,
    ) -> Optional[MemoryItem]:
        """Persist *why* a proposed change was rejected, so it isn't repeated."""
        return self._write(
            repo=repo,
            content=content,
            kind=MemoryKind.REJECTION_LESSON,
            workspace_id=workspace_id,
            repository_id=repository_id,
            source_job_id=source_job_id,
            metadata=metadata,
        )


    def record_reviewed_intelligence(
        self,
        *,
        repo: str,
        source_job_id: str,
        source_run_id: str,
        outcome_id: int,
        outcome_decision: str,
        content: str,
        kind: str,
        workspace_id: Optional[str] = None,
        repository_id: Optional[str] = None,
        metadata: Optional[Dict[str, object]] = None,
        item_key: Optional[str] = None,
    ) -> Optional[MemoryItem]:
        """Atomically persist reviewed intelligence and its provenance."""
        text = (content or "").strip()
        if not text:
            return None
        memory_kind = kind if kind in MemoryKind.ALL else MemoryKind.ACCEPTED_CHANGE
        key = reviewed_item_key(kind=memory_kind, content=text, item_key=item_key)
        content_hash = reviewed_content_hash(text)
        record = MemoryRecord(
            repo=repo,
            content=text,
            kind=memory_kind,
            metadata=dict(metadata or {}),
            approved=True,
            workspace_id=workspace_id,
            repository_id=repository_id,
            source_job_id=source_job_id,
        )
        writer = getattr(self._provider, "write_with_provenance", None)
        if writer is None:
            written = self._provider.write(record)
        else:
            written = writer(
                record,
                {
                    "source_run_id": source_run_id,
                    "source_job_id": source_job_id,
                    "outcome_id": outcome_id,
                    "outcome_decision": outcome_decision,
                    "item_key": key,
                    "content_hash": content_hash,
                    "workspace_id": workspace_id,
                    "repository_id": repository_id,
                },
            )
        if written is None:
            return None
        return self._to_item(written, "recorded")

    def record_reviewed_intelligence_batch(
        self,
        *,
        repo: str,
        source_job_id: str,
        source_run_id: str,
        outcome_id: int,
        outcome_decision: str,
        items: Sequence[Dict[str, object]],
        workspace_id: Optional[str] = None,
        repository_id: Optional[str] = None,
        metadata: Optional[Dict[str, object]] = None,
    ) -> List[MemoryItem]:
        """Atomically persist multiple reviewed intelligence items."""
        records = []
        provenances = []
        base_meta = dict(metadata or {})
        seen_keys = set()
        for raw in items:
            content = str(raw.get("content", "")).strip()
            if not content:
                continue
            kind = str(raw.get("kind") or MemoryKind.ACCEPTED_CHANGE)
            memory_kind = kind if kind in MemoryKind.ALL else MemoryKind.ACCEPTED_CHANGE
            key = reviewed_item_key(
                kind=memory_kind,
                content=content,
                item_key=str(raw.get("item_key") or raw.get("id") or ""),
            )
            identity = (memory_kind, key)
            if identity in seen_keys:
                continue
            seen_keys.add(identity)
            item_meta = {**base_meta, **dict(raw.get("metadata") or {})}
            records.append(
                MemoryRecord(
                    repo=repo,
                    content=content,
                    kind=memory_kind,
                    metadata=item_meta,
                    approved=True,
                    workspace_id=workspace_id,
                    repository_id=repository_id,
                    source_job_id=source_job_id,
                )
            )
            provenances.append(
                {
                    "source_run_id": source_run_id,
                    "source_job_id": source_job_id,
                    "outcome_id": outcome_id,
                    "outcome_decision": outcome_decision,
                    "item_key": key,
                    "content_hash": reviewed_content_hash(content),
                    "workspace_id": workspace_id,
                    "repository_id": repository_id,
                }
            )
        if not records:
            return []
        writer = getattr(self._provider, "write_many_with_provenance", None)
        if writer is None:
            return [
                self._to_item(w, "recorded")
                for w in (self._provider.write(r) for r in records)
                if w is not None
            ]
        written = writer(records, provenances)
        return [self._to_item(w, "recorded") for w in written if w is not None]

    # -- internals --------------------------------------------------------
    def _write(
        self,
        *,
        repo: str,
        content: str,
        kind: str,
        workspace_id: Optional[str],
        repository_id: Optional[str],
        source_job_id: Optional[str],
        metadata: Optional[Dict[str, object]],
    ) -> Optional[MemoryItem]:
        text = (content or "").strip()
        if not text:
            return None
        record = MemoryRecord(
            repo=repo,
            content=text,
            kind=kind,
            metadata=dict(metadata or {}),
            approved=True,
            workspace_id=workspace_id,
            repository_id=repository_id,
            source_job_id=source_job_id,
        )
        written = self._provider.write(record)
        if written is None:
            return None
        return self._to_item(written, "recorded")

    @staticmethod
    def _reason(matched: List[str], standing: bool) -> str:
        if matched:
            head = "matched: " + ", ".join(matched[:5])
            return head + ("; standing rule" if standing else "")
        return "standing rule" if standing else "relevant"

    @staticmethod
    def _to_item(rec: MemoryRecord, selection_reason: str) -> MemoryItem:
        return MemoryItem(
            memory_id=rec.memory_id or "",
            kind=rec.kind or "note",
            content=rec.content,
            repository_id=rec.repository_id,
            workspace_id=rec.workspace_id,
            source_job_id=rec.source_job_id,
            created_at=rec.created_at,
            selection_reason=selection_reason,
            metadata=dict(rec.metadata or {}),
        )
