"""Immutable source delivery with preflight before StreamingResponse starts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterator, Optional

from ..github_app import GitHubApp
from .github import ExecutorGitHub
from .models import ExecutionRunRecord, ExecutionStatus, FailureCategory

_CHUNK = 1024 * 256


def sanitize_source_error(text: object) -> str:
    """Remove tokens and credential-bearing URLs from source-delivery errors."""
    raw = str(text)
    raw = re.sub(r"x-access-token:[^@\s]+@", "x-access-token:***@", raw)
    raw = re.sub(r"(token |Bearer )[A-Za-z0-9_\-.]+", r"\1***", raw, flags=re.I)
    raw = re.sub(r"gh[opsu]_[A-Za-z0-9_]+", "***", raw)
    return raw[:500]


class SourceError(RuntimeError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(sanitize_source_error(message))
        self.status = status


@dataclass
class PreparedSource:
    response: object
    first_chunk: bytes
    max_bytes: int
    total: int = 0
    closed: bool = False

    def close(self) -> None:
        if self.closed:
            return
        close = getattr(self.response, "close", None)
        if close:
            close()
        self.closed = True

    def iter_bytes(self) -> Iterator[bytes]:
        try:
            if self.first_chunk:
                self.total += len(self.first_chunk)
                yield self.first_chunk
            while True:
                chunk = self.response.read(_CHUNK)
                if not chunk:
                    break
                self.total += len(chunk)
                if self.total > self.max_bytes:
                    raise SourceError(f"source exceeds {self.max_bytes} bytes", status=413)
                yield chunk
        except SourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SourceError(f"source stream failed: {exc}", status=502) from exc
        finally:
            self.close()


def _customer_installation_id(run: ExecutionRunRecord) -> Optional[int]:
    from .. import workspaces as ws

    if not run.repository_id or not run.workspace_id:
        return None
    repo = ws.get_repository(run.workspace_id, run.repository_id)
    if repo is None:
        return None
    inst = ws.get_installation_by_record_id(repo.github_installation_record_id)
    return inst.github_installation_id if inst else None


def _content_length(resp: object) -> Optional[int]:
    headers = getattr(resp, "headers", {}) or {}
    raw = None
    if hasattr(headers, "get"):
        raw = headers.get("Content-Length") or headers.get("content-length")
    if raw is None and hasattr(resp, "getheader"):
        raw = resp.getheader("Content-Length")
    try:
        return int(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


def prepare_source(
    settings,
    run: ExecutionRunRecord,
    repo_full_name: str,
    *,
    app: Optional[GitHubApp] = None,
    open_archive: Optional[Callable] = None,
) -> PreparedSource:
    """Open and validate source before the single-use download is claimed."""
    max_bytes = settings.executor_source_max_bytes
    installation_id = _customer_installation_id(run)
    if installation_id is None:
        raise SourceError("customer installation not resolvable", status=409)

    owner, _, name = repo_full_name.partition("/")
    app = app or GitHubApp(
        app_id=settings.github_app_id,
        private_key=settings.github_app_private_key,
        installation_id="0",
    )
    github = ExecutorGitHub(app)
    try:
        token_data = github.scoped_installation_token(
            installation_id,
            repositories=[name],
            permissions={"contents": "read"},
        )
        token = token_data["token"]
        opener = open_archive or (lambda o, n, s, t: github.open_tarball(o, n, s, t))
        resp = opener(owner, name, run.base_sha, token)
    except SourceError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SourceError(f"could not open source archive: {exc}", status=502) from exc

    try:
        status = getattr(resp, "status", None) or getattr(resp, "code", None)
        if status is not None and int(status) >= 400:
            raise SourceError(f"source archive returned HTTP {status}", status=502)
        length = _content_length(resp)
        if length is not None and length > max_bytes:
            raise SourceError(f"source exceeds {max_bytes} bytes", status=413)
        try:
            first = resp.read(_CHUNK)
        except Exception as exc:  # noqa: BLE001
            raise SourceError(f"could not read source archive: {exc}", status=502) from exc
        if first and len(first) > max_bytes:
            raise SourceError(f"source exceeds {max_bytes} bytes", status=413)
        return PreparedSource(resp, first or b"", max_bytes)
    except Exception:
        close = getattr(resp, "close", None)
        if close:
            close()
        raise


def stream_source(settings, run: ExecutionRunRecord, repo_full_name: str, **kwargs) -> Iterator[bytes]:
    return prepare_source(settings, run, repo_full_name, **kwargs).iter_bytes()


def fail_streaming_source(
    prepared: PreparedSource,
    exec_store,
    run: ExecutionRunRecord,
    job_store,
    reason: str,
) -> None:
    prepared.close()
    exec_store.set_status(
        run.id,
        ExecutionStatus.FAILED,
        failure_category=FailureCategory.EXECUTOR_ERROR,
    )
    exec_store.revoke_token(run.id)
    from ...orchestration.status import JobStatus, is_terminal

    job = job_store.get_job(run.job_id)
    if job is not None and not is_terminal(job.status):
        job_store.set_status(run.job_id, JobStatus.FAILED, error=sanitize_source_error(reason))
