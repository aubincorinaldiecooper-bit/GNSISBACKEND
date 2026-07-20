"""Versioned Genesis/Ponytail system policy, stored as a durable resource.

The agent's *trusted* system policy — the Genesis identity + the Ponytail
decision ladder (see :mod:`gnsis.agent.policy`) — is committed into the existing
versioned resource store (:class:`PostgresResourceStore`) under a stable
``(kind, name)``. That gives every run an exact, hash-identified policy version
it can be pinned to forever, and gives us auditable lineage when the policy
evolves.

Two hashes are in play, deliberately:

* the resource store's own ``content_hash`` (canonical-JSON) is lineage
  bookkeeping — "did this version actually change?";
* :func:`policy_content_hash` is a plain SHA-256 over the *exact prompt bytes*
  the executor will use as its system message. The RunSpec carries this one and
  the executor recomputes it to verify the policy wasn't altered in transit.

Policy is **never** auto-promoted here. Only :func:`seed_default_policy` (the
human-authored ladder) writes a version in this phase; a DSPy-optimized candidate
(a later phase) must be promoted by an explicit, separate action — resolving the
"active" policy always returns the committed head, never a generated draft.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Optional

from ..agent.policy import build_system_prompt
from ..resources.resource import ResourceVersion
from .repository import PostgresResourceStore

#: Stable coordinates of the trusted coding policy in the resource store.
POLICY_KIND = "agent_policy"
POLICY_NAME = "genesis-coding-policy"


def policy_content_hash(text: str) -> str:
    """Plain SHA-256 over the exact prompt bytes (the executor verifies this)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ResolvedPolicy:
    """A concrete policy version ready to hand to a run."""

    name: str
    version: int
    content: str
    content_hash: str
    parent_version: Optional[int]

    def to_public_dict(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "content": self.content,
            "content_hash": self.content_hash,
            "parent_version": self.parent_version,
        }


def _from_version(v: ResourceVersion) -> ResolvedPolicy:
    # Stored content is the prompt string; tolerate anything else by stringifying.
    content = v.content if isinstance(v.content, str) else str(v.content)
    return ResolvedPolicy(
        name=v.name,
        version=v.version,
        content=content,
        content_hash=policy_content_hash(content),
        parent_version=v.parent_version,
    )


def seed_default_policy(store: Optional[PostgresResourceStore] = None) -> ResolvedPolicy:
    """Ensure v1 of the trusted policy exists; return the head. Idempotent.

    Seeds from :func:`gnsis.agent.policy.build_system_prompt` — the single source
    of truth for the Genesis identity + Ponytail ladder — so the versioned policy
    and the in-process native prompt never drift at seed time.
    """
    store = store or PostgresResourceStore()
    head = store.head(POLICY_KIND, POLICY_NAME)
    if head is not None:
        return _from_version(head)
    prompt = build_system_prompt()
    version = store.commit(
        POLICY_KIND,
        POLICY_NAME,
        prompt,
        message="Seed Genesis/Ponytail coding policy v1 (from agent.policy ladder)",
    )
    return _from_version(version)


def resolve_active_policy(store: Optional[PostgresResourceStore] = None) -> ResolvedPolicy:
    """The policy version a new run should be pinned to (the committed head)."""
    store = store or PostgresResourceStore()
    head = store.head(POLICY_KIND, POLICY_NAME)
    if head is None:
        return seed_default_policy(store)
    return _from_version(head)


def get_policy_version(
    version: int, store: Optional[PostgresResourceStore] = None
) -> Optional[ResolvedPolicy]:
    """Reconstruct a specific pinned version (retry-stable / historical audit)."""
    store = store or PostgresResourceStore()
    for v in store.history(POLICY_KIND, POLICY_NAME):
        if v.version == version:
            return _from_version(v)
    return None
