"""GNSIS's HTTP boundary around upstream Omni-SimpleMem.

Ported from Clipit's hardened sidecar (CLIPIT ``tools/simplemem/sidecar.py``,
merged through PRs #106/#107/#109/#114/#123/#126). The product contract it adds
over the upstream package is reshaped for GNSIS: instead of one namespace per
Clipit video, a namespace is one GNSIS memory scope (for example ``repo:owner/name``
or ``workspace:<id>``), and every memory carries an explicit type plus
structured provenance instead of only frame coordinates.

Kept intact from the Clipit implementation:

- the transformers 4.57 CLIP contract check (transformers 5.x makes
  ``get_image_features`` return a ``BaseModelOutputWithPooling`` and upstream
  then stores EMPTY embeddings while reporting success);
- a shared CLIP visual/text embedding space with an explicit embedding-version
  gate, so a store written under another contract is never served;
- durable S3-compatible archives: content-addressed generations, a manifest
  that commits last, sha256 verification on restore, and safe extraction;
- the ``.previous`` cache swap so an interrupted rewrite never destroys the
  last committed memory;
- restore-on-cache-miss and a bounded local cache (local disk is cache, not
  the source of truth);
- caption accounting for image/video frames through ``captions.py``;
- an authenticated service boundary.

Memory types are explicit and checked at the boundary:
``episode``, ``visual_event``, ``audio_event``, ``fact``, ``decision``,
``preference``, ``task_result``, ``approved_code_intelligence``. Episodic
records keep source-time ranges and source references; semantic records keep a
provenance pointer to whatever produced them (for example a Postgres
CodeMemory ``memory_id`` for ``approved_code_intelligence``).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import shutil
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

import simplemem
from simplemem import create
from simplemem.multimodal.core.config import OmniMemoryConfig
import transformers


def _assert_transformers_contract() -> None:
    """Refuse to serve on a transformers release whose CLIP API upstream cannot read.

    Upstream calls ``CLIPModel.get_image_features(...)`` and then ``.cpu()`` on the
    result. On transformers 4.57 that is a tensor. From 5.0 it is a
    ``BaseModelOutputWithPooling``: ``.cpu()`` raises inside upstream's ``try``,
    which logs the error and returns an EMPTY embedding. Every frame is then
    remembered without a vector, every query finds nothing, and indexing reports
    success throughout. A failed boot is the honest version of that.
    """
    version = str(getattr(transformers, "__version__", "0"))
    head = version.split(".")[0]
    major = int(head) if head.isdigit() else 0
    if major >= 5:
        raise RuntimeError(
            f"transformers {version} is installed, but upstream Omni-SimpleMem's CLIP path needs 4.57.x "
            "(get_image_features must return a tensor, not BaseModelOutputWithPooling). "
            "Pin it in tools/simplemem/requirements.txt and rebuild the image."
        )


_assert_transformers_contract()

sys.path.insert(0, str(Path(__file__).resolve().parent))
from captions import CaptionWriter  # noqa: E402


DATA_ROOT = Path(os.environ.get("SIMPLEMEM_DATA_DIR", "/data/simplemem")).resolve()
DATA_ROOT.mkdir(parents=True, exist_ok=True)
META_FILE = "gnsis_meta.json"
ARCHIVE_MARKER_FILE = ".gnsis_archived.json"
SHARED_CLIP_MODEL = os.environ.get("SIMPLEMEM_CLIP_MODEL", "openai/clip-vit-base-patch32")
SHARED_CLIP_DIM = int(os.environ.get("SIMPLEMEM_CLIP_DIM", "512"))
PROCESS_AUDIO = os.environ.get("SIMPLEMEM_PROCESS_AUDIO", "false").lower() in {"1", "true", "yes", "on"}
ARCHIVE_REQUIRED = os.environ.get("SIMPLEMEM_ARCHIVE_REQUIRED", "false").lower() in {"1", "true", "yes", "on"}
ARCHIVE_PREFIX = os.environ.get("SIMPLEMEM_ARCHIVE_PREFIX", "gnsis-simplemem/v1").strip("/") or "gnsis-simplemem/v1"
CACHE_HIGH_WATER_BYTES = int(os.environ.get("SIMPLEMEM_CACHE_HIGH_WATER_BYTES", str(4 * 1024**3)))
CACHE_LOW_WATER_BYTES = int(os.environ.get("SIMPLEMEM_CACHE_LOW_WATER_BYTES", str(3 * 1024**3)))
ARCHIVE_SCHEMA_VERSION = 1
META_SCHEMA_VERSION = 1
# The embedding contract every memory is written and read under. It is bumped
# when what a stored vector means changes; v2 marks the transformers 4.57 pin,
# because memories written under transformers 5.x carried no CLIP vectors at
# all. A deployment that overrides it does not get a quietly different memory
# store: the sidecar refuses to start.
EXPECTED_EMBEDDING_VERSION = "v2-transformers457"
EMBEDDING_VERSION = (
    os.environ.get("SIMPLEMEM_EMBEDDING_VERSION", EXPECTED_EMBEDDING_VERSION).strip() or EXPECTED_EMBEDDING_VERSION
)
if EMBEDDING_VERSION != EXPECTED_EMBEDDING_VERSION:
    raise RuntimeError(
        f"SIMPLEMEM_EMBEDDING_VERSION={EMBEDDING_VERSION!r}, but this sidecar writes and reads memories under "
        f"{EXPECTED_EMBEDDING_VERSION!r}. A different value would let it serve memories written under another "
        "contract (v1 memories have no CLIP vectors). Unset the variable or set it to the expected contract."
    )
MAX_UPLOAD_BYTES = int(os.environ.get("SIMPLEMEM_MAX_UPLOAD_BYTES", str(2 * 1024**3)))
MAX_PROVENANCE_BYTES = int(os.environ.get("SIMPLEMEM_MAX_PROVENANCE_BYTES", str(16 * 1024)))
INTERNAL_TOKEN = os.environ.get("SIMPLEMEM_INTERNAL_TOKEN", "").strip()

# The memory types GNSIS supports per AGENTS.md locked decision 3.
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

operation_lock = asyncio.Lock()
app = FastAPI(title="GNSIS Omni-SimpleMem", version="1")
_s3: Any | None = None
log = logging.getLogger("uvicorn.error")


@app.middleware("http")
async def _telemetry(request: Any, call_next: Any) -> Any:
    start = time.monotonic()
    response = await call_next(request)
    # One structured line per request: the live observability hook for
    # writes, queries, recalls, and archive syncs.
    log.info(
        "sidecar.request method=%s path=%s status=%s latency_ms=%.1f",
        request.method,
        request.url.path,
        response.status_code,
        (time.monotonic() - start) * 1000,
    )
    return response


class QueryBody(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=20, ge=1, le=200)
    types: list[str] | None = None
    # ``limit``/``scope`` are accepted (and ignored) so the runtime's generic
    # HttpMemoryProvider payload also lands here unchanged.
    limit: int | None = None
    scope: str | None = None


class TextBody(BaseModel):
    text: str = Field(min_length=1, max_length=64_000)
    type: str
    summary: str | None = Field(default=None, max_length=4000)
    session_id: str | None = Field(default=None, max_length=200)
    tags: list[str] | None = None
    force: bool = False
    provenance: dict[str, Any] | None = None


def _safe_namespace(value: str) -> str:
    cleaned = value.strip()
    if not cleaned or len(cleaned) > 200:
        raise HTTPException(status_code=400, detail="invalid namespace")
    if any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_:.@/" for ch in cleaned):
        raise HTTPException(status_code=400, detail="invalid namespace")
    if ".." in cleaned:
        raise HTTPException(status_code=400, detail="invalid namespace")
    return cleaned.replace("/", "__")


def _namespace_dir(namespace: str) -> Path:
    return DATA_ROOT / _safe_namespace(namespace)


def _check_type(value: str) -> str:
    memory_type = str(value or "").strip()
    if memory_type not in MEMORY_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"unknown memory type {memory_type!r}; expected one of {sorted(MEMORY_TYPES)}",
        )
    return memory_type


def _check_provenance(value: dict[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="provenance must be an object")
    if len(json.dumps(value, default=str).encode("utf-8")) > MAX_PROVENANCE_BYTES:
        raise HTTPException(status_code=413, detail="provenance exceeds the byte limit")
    return value


def _config() -> OmniMemoryConfig:
    config = OmniMemoryConfig()
    api_key = os.environ.get("SIMPLEMEM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    api_base = os.environ.get("SIMPLEMEM_API_BASE") or os.environ.get("OPENAI_API_BASE")
    if api_key:
        config.llm.api_key = api_key
    if api_base:
        config.llm.api_base_url = api_base

    config.llm.caption_model = os.environ.get("SIMPLEMEM_CAPTION_MODEL", config.llm.caption_model)
    config.llm.summary_model = os.environ.get("SIMPLEMEM_SUMMARY_MODEL", config.llm.summary_model)
    config.llm.query_model = os.environ.get("SIMPLEMEM_QUERY_MODEL", config.llm.query_model)
    config.llm.whisper_model = os.environ.get("SIMPLEMEM_TRANSCRIPTION_MODEL", config.llm.whisper_model)

    # One cross-modal space. Upstream's HybridVectorStore makes visual/video
    # MAUs text-searchable when text and visual dimensions match.
    config.embedding.model_name = SHARED_CLIP_MODEL
    config.embedding.embedding_dim = SHARED_CLIP_DIM
    config.embedding.visual_embedding_model = SHARED_CLIP_MODEL
    config.embedding.visual_embedding_dim = SHARED_CLIP_DIM
    config.entropy_trigger.visual_encoder = "clip"
    config.entropy_trigger.visual_model_name = SHARED_CLIP_MODEL
    return config


def _models(config: OmniMemoryConfig) -> dict[str, str]:
    return {
        "caption": config.llm.caption_model,
        "visual": config.embedding.visual_embedding_model,
        "text_embedding": config.embedding.model_name,
        "transcription": config.llm.whisper_model if PROCESS_AUDIO else "disabled",
    }


def _meta_path(memory_dir: Path) -> Path:
    return memory_dir / META_FILE


def _empty_meta(namespace: str) -> dict[str, Any]:
    return {
        "schemaVersion": META_SCHEMA_VERSION,
        "namespace": namespace,
        "crossModalModel": SHARED_CLIP_MODEL,
        "crossModalDim": SHARED_CLIP_DIM,
        "embeddingVersion": EMBEDDING_VERSION,
        # mauId -> {type, summary, createdAtUnix, provenance}
        "memories": {},
        # mauId -> {frameIndex, seconds} for video memories
        "frames": {},
    }


def _write_meta(memory_dir: Path, payload: dict[str, Any]) -> None:
    target = _meta_path(memory_dir)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    temporary.replace(target)


def _read_meta(memory_dir: Path) -> dict[str, Any]:
    target = _meta_path(memory_dir)
    if not target.exists():
        raise HTTPException(status_code=409, detail="namespace has no GNSIS metadata")
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail="namespace metadata is unreadable") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("memories"), dict):
        raise HTTPException(status_code=500, detail="namespace metadata is invalid")
    return raw


def _assert_meta_compatible(meta: dict[str, Any]) -> None:
    if (
        meta.get("crossModalModel") != SHARED_CLIP_MODEL
        or meta.get("crossModalDim") != SHARED_CLIP_DIM
        or meta.get("embeddingVersion") != EMBEDDING_VERSION
    ):
        raise HTTPException(
            status_code=409,
            detail="memory embedding identity changed; the namespace must be rebuilt",
        )


def _record_memory(
    memory_dir: Path,
    namespace: str,
    *,
    mau_id: str,
    memory_type: str,
    summary: str,
    provenance: dict[str, Any],
) -> None:
    try:
        meta = _read_meta(memory_dir)
    except HTTPException:
        meta = _empty_meta(namespace)
    meta["memories"][mau_id] = {
        "type": memory_type,
        "summary": summary[:2000],
        "createdAtUnix": int(time.time()),
        "provenance": provenance,
    }
    _write_meta(memory_dir, meta)


def _update_meta(memory_dir: Path, namespace: str, **changes: Any) -> None:
    try:
        meta = _read_meta(memory_dir)
    except HTTPException:
        meta = _empty_meta(namespace)
    meta.update(changes)
    _write_meta(memory_dir, meta)


class IndexingRefused(RuntimeError):
    """Indexing that finished but must not be called a memory; the message says why."""


def _open_memory(memory_dir: Path):
    config = _config()
    memory = create(mode="omni", config=config, data_dir=str(memory_dir))
    # Frame captions go through the hardened writer: same prompt and model as
    # upstream, but a blank reply is asked again with more room, and a caption
    # that never arrives is counted instead of stored as "Image captured".
    processor = memory.video_processor.image_processor
    writer = CaptionWriter(
        processor._get_llm_client,
        processor._normalize_model(config.llm.caption_model),
        load_image=processor._load_image,
        log=log,
    )
    processor.generate_summary = writer.caption
    processor.gnsis_captions = writer
    if not PROCESS_AUDIO:
        memory.video_processor.process_audio = False
        memory.video_processor.audio_processor = None
    return memory


def _caption_stats(memory: Any) -> dict[str, Any]:
    writer = getattr(memory.video_processor.image_processor, "gnsis_captions", None)
    if writer is None:
        return {"attempted": 0, "captioned": 0, "retried": 0, "failed": 0, "lastError": "captions were not routed through CaptionWriter"}
    return writer.stats.as_dict()


def _modality(item: dict[str, Any]) -> str:
    value = str(item.get("modality_type") or item.get("modality") or "text").lower()
    return value if value in {"text", "visual", "audio", "video", "multimodal"} else "text"


def _env(name: str, fallback: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is not None and value.strip():
        return value.strip()
    if fallback:
        value = os.environ.get(fallback)
        if value is not None and value.strip():
            return value.strip()
    return None


def _archive_bucket() -> str | None:
    return _env("SIMPLEMEM_ARCHIVE_BUCKET", "BUCKET_NAME")


def _archive_configured() -> bool:
    return bool(
        _archive_bucket()
        and _env("SIMPLEMEM_ARCHIVE_ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID")
        and _env("SIMPLEMEM_ARCHIVE_SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY")
    )


def _assert_archive_configuration() -> None:
    if ARCHIVE_REQUIRED and not _archive_configured():
        raise RuntimeError("durable SimpleMem archive is required but S3-compatible storage is not configured")
    if CACHE_LOW_WATER_BYTES <= 0 or CACHE_HIGH_WATER_BYTES <= CACHE_LOW_WATER_BYTES:
        raise RuntimeError("SimpleMem cache watermarks are invalid")
    if MAX_UPLOAD_BYTES <= 0:
        raise RuntimeError("SimpleMem upload byte limit is invalid")


def _assert_internal_token_configuration() -> None:
    if len(INTERNAL_TOKEN) < 32:
        raise RuntimeError("SIMPLEMEM_INTERNAL_TOKEN must be configured with at least 32 characters")


def _authorize_internal(
    token: str | None = Header(default=None, alias="X-GNSIS-SimpleMem-Token"),
) -> None:
    _assert_internal_token_configuration()
    if token is None or not hmac.compare_digest(token, INTERNAL_TOKEN):
        raise HTTPException(status_code=401, detail="unauthorized")


def _s3_client():
    global _s3
    if _s3 is not None:
        return _s3
    _assert_archive_configuration()
    if not _archive_configured():
        return None
    force_path = (_env("SIMPLEMEM_ARCHIVE_FORCE_PATH_STYLE", "S3_FORCE_PATH_STYLE") or "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    _s3 = boto3.client(
        "s3",
        endpoint_url=_env("SIMPLEMEM_ARCHIVE_ENDPOINT_URL", "AWS_ENDPOINT_URL"),
        region_name=_env("SIMPLEMEM_ARCHIVE_REGION", "AWS_REGION") or "us-east-1",
        aws_access_key_id=_env("SIMPLEMEM_ARCHIVE_ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=_env("SIMPLEMEM_ARCHIVE_SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY"),
        config=BotoConfig(s3={"addressing_style": "path" if force_path else "auto"}),
    )
    return _s3


def _archive_base(namespace: str) -> str:
    return f"{ARCHIVE_PREFIX}/{_safe_namespace(namespace)}"


def _manifest_key(namespace: str) -> str:
    return f"{_archive_base(namespace)}/manifest.json"


def _archive_key(namespace: str, digest: str) -> str:
    return f"{_archive_base(namespace)}/archives/{digest}.tar.gz"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _archive_marker(memory_dir: Path) -> Path:
    return memory_dir / ARCHIVE_MARKER_FILE


def _mark_archived(memory_dir: Path, manifest: dict[str, Any]) -> None:
    _archive_marker(memory_dir).write_text(json.dumps(manifest, separators=(",", ":")), encoding="utf-8")


def _has_archive_marker(memory_dir: Path) -> bool:
    return _archive_marker(memory_dir).exists()


def _load_manifest(namespace: str) -> dict[str, Any] | None:
    client = _s3_client()
    if client is None:
        return None
    bucket = _archive_bucket()
    assert bucket is not None
    try:
        response = client.get_object(Bucket=bucket, Key=_manifest_key(namespace))
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise
    raw = response["Body"].read()
    manifest = json.loads(raw.decode("utf-8"))
    digest = manifest.get("sha256") if isinstance(manifest, dict) else None
    expected_archive_key = _archive_key(namespace, digest) if isinstance(digest, str) else None
    if (
        not isinstance(manifest, dict)
        or manifest.get("schemaVersion") != ARCHIVE_SCHEMA_VERSION
        or manifest.get("namespace") != namespace
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(ch not in "0123456789abcdef" for ch in digest.lower())
        or manifest.get("archiveKey") != expected_archive_key
    ):
        raise RuntimeError("SimpleMem archive manifest is invalid")
    return manifest


def _read_archive_marker(memory_dir: Path) -> dict[str, Any] | None:
    marker = _archive_marker(memory_dir)
    if not marker.exists():
        return None
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _cache_ready(memory_dir: Path, expected_namespace: str | None = None) -> bool:
    if not memory_dir.is_dir() or not _meta_path(memory_dir).exists():
        return False
    # When durable archives are configured, only a cache entry backed by a
    # committed manifest marker is eligible to suppress archive restoration.
    if _archive_configured():
        marker = _read_archive_marker(memory_dir)
        if marker is None or marker.get("namespace") != (expected_namespace or memory_dir.name):
            return False
    try:
        # A valid upstream memory contains more than the two metadata files.
        # This is intentionally format-agnostic; opening it below is the final
        # validation and triggers archive recovery if upstream rejects it.
        return any(
            child.name not in {META_FILE, ARCHIVE_MARKER_FILE}
            for child in memory_dir.iterdir()
        )
    except OSError:
        return False


def _prepare_cache_for_query(namespace: str) -> tuple[Path, bool]:
    final_dir = _namespace_dir(namespace)
    backup_dir = final_dir.with_name(f"{final_dir.name}.previous")
    if _cache_ready(final_dir, namespace):
        return final_dir, False

    if final_dir.exists():
        shutil.rmtree(final_dir, ignore_errors=True)

    # A process death during replacement can leave the previous committed cache
    # parked beside the final path. Recover it before paying for an S3 restore.
    if _cache_ready(backup_dir, namespace):
        backup_dir.replace(final_dir)
        return final_dir, False
    if backup_dir.exists():
        shutil.rmtree(backup_dir, ignore_errors=True)

    restored = _restore_sync(namespace)
    if not restored:
        raise HTTPException(status_code=404, detail="namespace memory not found")
    if not _cache_ready(final_dir, namespace):
        shutil.rmtree(final_dir, ignore_errors=True)
        raise RuntimeError("restored SimpleMem cache is incomplete")
    return final_dir, True


def _archive_sync(namespace: str, memory_dir: Path) -> dict[str, Any] | None:
    client = _s3_client()
    if client is None:
        return None
    bucket = _archive_bucket()
    assert bucket is not None

    previous = _load_manifest(namespace)
    previous_key = previous.get("archiveKey") if previous else None
    fd, raw_path = tempfile.mkstemp(prefix=f"gnsis-simplemem-{_safe_namespace(namespace)}-", suffix=".tar.gz")
    os.close(fd)
    archive_path = Path(raw_path)
    new_key: str | None = None
    try:
        # This marker describes the cache copy, not the memory itself.
        _archive_marker(memory_dir).unlink(missing_ok=True)
        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(memory_dir, arcname="memory")
        digest = _sha256(archive_path)
        new_key = _archive_key(namespace, digest)
        size = archive_path.stat().st_size

        # Generation objects are immutable/content-addressed. The old manifest
        # continues to point at the old bytes until the new manifest commits.
        client.upload_file(str(archive_path), bucket, new_key)
        manifest = {
            "schemaVersion": ARCHIVE_SCHEMA_VERSION,
            "namespace": namespace,
            "archiveKey": new_key,
            "sha256": digest,
            "sizeBytes": size,
            "createdAtUnix": int(time.time()),
        }
        client.put_object(
            Bucket=bucket,
            Key=_manifest_key(namespace),
            Body=json.dumps(manifest, separators=(",", ":")).encode("utf-8"),
            ContentType="application/json",
        )
        _mark_archived(memory_dir, manifest)

        # Only after the new manifest is durable may the previous generation
        # be removed. Failure here leaks bytes but cannot corrupt the live copy.
        if previous_key and previous_key != new_key:
            try:
                client.delete_object(Bucket=bucket, Key=previous_key)
            except Exception as exc:
                log.warning("could not delete previous SimpleMem archive generation: %s", type(exc).__name__)
        return manifest
    except Exception:
        # Once the generation upload succeeded and the manifest write was
        # attempted, an exception cannot tell us whether S3 rejected the write
        # or committed it and only lost the response. Deleting the new
        # generation here could therefore delete the bytes referenced by a
        # successfully committed manifest. Leave a possible orphan instead; a
        # later successful replacement/delete can clean it safely.
        raise
    finally:
        archive_path.unlink(missing_ok=True)


def _safe_extract(tar: tarfile.TarFile, destination: Path) -> None:
    destination_resolved = destination.resolve()
    for member in tar.getmembers():
        target = (destination / member.name).resolve()
        if destination_resolved != target and destination_resolved not in target.parents:
            raise RuntimeError("SimpleMem archive contains an unsafe path")
        if member.issym() or member.islnk():
            raise RuntimeError("SimpleMem archive contains a link")
    tar.extractall(destination)


def _restore_sync(namespace: str) -> bool:
    client = _s3_client()
    if client is None:
        return False
    manifest = _load_manifest(namespace)
    if manifest is None:
        return False
    bucket = _archive_bucket()
    assert bucket is not None

    fd, raw_path = tempfile.mkstemp(prefix=f"gnsis-simplemem-restore-{_safe_namespace(namespace)}-", suffix=".tar.gz")
    os.close(fd)
    archive_path = Path(raw_path)
    restore_root = Path(tempfile.mkdtemp(prefix=f".{_safe_namespace(namespace)}.restoring-", dir=DATA_ROOT))
    final_dir = _namespace_dir(namespace)
    try:
        client.download_file(bucket, manifest["archiveKey"], str(archive_path))
        if _sha256(archive_path) != manifest["sha256"]:
            raise RuntimeError("SimpleMem archive checksum mismatch")
        with tarfile.open(archive_path, "r:gz") as tar:
            _safe_extract(tar, restore_root)
        extracted = restore_root / "memory"
        if not extracted.is_dir() or not _meta_path(extracted).exists():
            raise RuntimeError("SimpleMem archive is missing required memory metadata")
        if final_dir.exists():
            shutil.rmtree(final_dir, ignore_errors=True)
        extracted.replace(final_dir)
        _mark_archived(final_dir, manifest)
        _touch_cache_entry(final_dir)
        return True
    finally:
        archive_path.unlink(missing_ok=True)
        shutil.rmtree(restore_root, ignore_errors=True)


def _delete_archive_sync(namespace: str) -> None:
    client = _s3_client()
    if client is None:
        return
    bucket = _archive_bucket()
    assert bucket is not None

    # Remove the manifest first so a failed cleanup cannot make an archived
    # memory restorable again. Retries discover leftover generations by prefix.
    client.delete_object(Bucket=bucket, Key=_manifest_key(namespace))
    prefix = f"{_archive_base(namespace)}/"
    continuation: str | None = None
    while True:
        kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": 1000}
        if continuation:
            kwargs["ContinuationToken"] = continuation
        page = client.list_objects_v2(**kwargs)
        keys = [item.get("Key") for item in page.get("Contents", []) if isinstance(item.get("Key"), str)]
        if keys:
            response = client.delete_objects(
                Bucket=bucket,
                Delete={"Objects": [{"Key": key} for key in keys], "Quiet": True},
            )
            errors = response.get("Errors") or []
            if errors:
                raise RuntimeError(f"failed to delete {len(errors)} SimpleMem archive objects")
        if not page.get("IsTruncated"):
            break
        continuation = page.get("NextContinuationToken")
        if not isinstance(continuation, str) or not continuation:
            raise RuntimeError("SimpleMem archive listing truncated without a continuation token")


def _directory_size(path: Path) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except FileNotFoundError:
                pass
    return total


def _touch_cache_entry(memory_dir: Path) -> None:
    now = time.time()
    try:
        os.utime(memory_dir, (now, now))
    except FileNotFoundError:
        pass


def _cache_entries() -> list[Path]:
    entries: list[Path] = []
    for candidate in DATA_ROOT.iterdir():
        if not candidate.is_dir() or candidate.name.startswith(".") or candidate.name.endswith(".previous"):
            continue
        entries.append(candidate)
    return entries


def _enforce_cache_budget() -> dict[str, int]:
    if not _archive_configured():
        size = _directory_size(DATA_ROOT)
        return {"beforeBytes": size, "afterBytes": size, "evicted": 0}
    before = _directory_size(DATA_ROOT)
    if before <= CACHE_HIGH_WATER_BYTES:
        return {"beforeBytes": before, "afterBytes": before, "evicted": 0}

    candidates: list[tuple[float, Path]] = []
    for entry in _cache_entries():
        if not _has_archive_marker(entry):
            continue
        try:
            touched = entry.stat().st_mtime
        except FileNotFoundError:
            continue
        candidates.append((touched, entry))
    candidates.sort(key=lambda pair: pair[0])

    current = before
    evicted = 0
    for _, entry in candidates:
        if current <= CACHE_LOW_WATER_BYTES:
            break
        size = _directory_size(entry)
        shutil.rmtree(entry, ignore_errors=True)
        current = max(0, current - size)
        evicted += 1
    return {"beforeBytes": before, "afterBytes": current, "evicted": evicted}


def _persisted(mau: Any) -> str | None:
    mau_id = getattr(mau, "id", None)
    return str(mau_id) if mau_id else None


def _write_text_sync(
    namespace: str,
    body: TextBody,
) -> dict[str, Any]:
    _assert_archive_configuration()
    started = time.perf_counter()
    memory_dir = _namespace_dir(namespace)
    backup_dir = memory_dir.with_name(f"{memory_dir.name}.previous")

    if not memory_dir.exists():
        restored = _restore_sync(namespace)
        if restored and not _cache_ready(memory_dir, namespace):
            shutil.rmtree(memory_dir, ignore_errors=True)
            raise RuntimeError("restored SimpleMem cache is incomplete")
    memory_dir.mkdir(parents=True, exist_ok=True)
    if not _meta_path(memory_dir).exists():
        # First write creates the namespace store.
        _write_meta(memory_dir, _empty_meta(namespace))
    _assert_meta_compatible(_read_meta(memory_dir))

    memory = None
    try:
        memory = _open_memory(memory_dir)
        result = memory.add_text(
            body.text,
            session_id=body.session_id or f"gnsis:{namespace}",
            tags=[*(body.tags or []), f"gnsis_type:{body.type}"],
            force=body.force,
        )
        if not result.success or result.mau is None:
            raise RuntimeError(result.error or "Omni-SimpleMem did not store the text memory")
        mau_id = _persisted(result.mau)
        summary = body.summary or getattr(result.mau, "content", None) or body.text
        memory.close()
        memory = None
        if mau_id:
            _record_memory(
                memory_dir,
                namespace,
                mau_id=mau_id,
                memory_type=body.type,
                summary=str(summary),
                provenance=body.provenance or {},
            )
        archive = _archive_sync(namespace, memory_dir)
        if ARCHIVE_REQUIRED and archive is None:
            raise RuntimeError("durable SimpleMem archive was required but was not written")
        _touch_cache_entry(memory_dir)
        shutil.rmtree(backup_dir, ignore_errors=True)
        cache = _enforce_cache_budget()
        return {
            "mauId": mau_id,
            "durableArchive": archive is not None,
            "cache": cache,
            "elapsedMs": round((time.perf_counter() - started) * 1000),
        }
    except Exception:
        if memory is not None:
            try:
                memory.close()
            except Exception:
                pass
        raise


def _media_sync(
    namespace: str,
    kind: str,
    source: Path,
    *,
    memory_type: str,
    provenance: dict[str, Any],
    session_id: str | None,
    tags: list[str],
    fps: float | None,
    max_frames: int | None,
    duration_seconds: float | None,
) -> dict[str, Any]:
    """Index one image/audio/video file under a namespace."""
    _assert_archive_configuration()
    started = time.perf_counter()
    final_dir = _namespace_dir(namespace)
    backup_dir = final_dir.with_name(f"{final_dir.name}.previous")

    if not _meta_path(final_dir).exists():
        restored = _restore_sync(namespace) if not final_dir.exists() else False
        if restored and not _cache_ready(final_dir, namespace):
            shutil.rmtree(final_dir, ignore_errors=True)
            raise RuntimeError("restored SimpleMem cache is incomplete")
    final_dir.mkdir(parents=True, exist_ok=True)
    if not _meta_path(final_dir).exists():
        _write_meta(final_dir, _empty_meta(namespace))

    memory = None
    try:
        memory = _open_memory(final_dir)
        tags = [*tags, f"gnsis_type:{memory_type}"]
        sid = session_id or f"gnsis:{namespace}"
        if kind == "video":
            assert fps is not None and max_frames is not None and duration_seconds is not None
            memory.video_processor.fps = fps
            result = memory.add_video(
                str(source),
                session_id=sid,
                tags=tags,
                max_frames=max_frames,
            )
        elif kind == "image":
            result = memory.add_image(str(source), session_id=sid, tags=tags)
        else:
            result = memory.add_audio(str(source), session_id=sid, tags=tags)

        if not result.success or result.mau is None:
            raise RuntimeError(result.error or f"Omni-SimpleMem did not create a {kind} memory")

        captions = _caption_stats(memory)
        if kind in {"video", "image"}:
            if captions["attempted"] > 0 and captions["captioned"] == 0:
                raise IndexingRefused(
                    f"no frame received a caption ({captions['attempted']} attempted; "
                    f"last error: {captions['lastError']})"
                )
            if captions["failed"] > 0:
                log.warning(
                    "SimpleMem captions missing for %d of %d frames of %s (last error: %s)",
                    captions["failed"], captions["attempted"], namespace, captions["lastError"],
                )

        metadata = result.metadata or {}
        meta = _read_meta(final_dir)
        if kind == "video":
            frame_maus = metadata.get("frame_maus") or []
            frames = dict(meta.get("frames") or {})
            assert fps is not None
            for frame in frame_maus:
                frame_id = str(getattr(frame, "id", "") or "")
                frame_meta = getattr(frame, "metadata", None)
                frame_index = getattr(frame_meta, "frame_index", None) if frame_meta is not None else None
                if not frame_id or not isinstance(frame_index, int) or frame_index < 0:
                    continue
                frames[frame_id] = {"frameIndex": frame_index, "seconds": frame_index / fps}
                meta["memories"].setdefault(
                    frame_id,
                    {
                        "type": memory_type,
                        "summary": str(getattr(frame, "caption", "") or "")[:2000],
                        "createdAtUnix": int(time.time()),
                        "provenance": provenance,
                    },
                )
            meta["frames"] = frames
            meta["lastVideo"] = {
                "fps": fps,
                "durationSeconds": duration_seconds,
                "framesProcessed": int(metadata.get("frames_processed") or len(frame_maus)),
                "framesSkipped": int(metadata.get("frames_skipped") or 0),
                "captions": captions,
            }
        mau_id = _persisted(result.mau)
        meta["memories"][mau_id or ""] = {
            "type": memory_type,
            "summary": str(getattr(result.mau, "content", "") or "")[:2000],
            "createdAtUnix": int(time.time()),
            "provenance": provenance,
        }
        meta["memories"].pop("", None)
        _write_meta(final_dir, meta)

        # close() flushes the upstream vector stores before durable snapshotting.
        memory.close()
        memory = None
        archive = _archive_sync(namespace, final_dir)
        if ARCHIVE_REQUIRED and archive is None:
            raise RuntimeError("durable SimpleMem archive was required but was not written")
        _touch_cache_entry(final_dir)
        shutil.rmtree(backup_dir, ignore_errors=True)
        cache = _enforce_cache_budget()

        reply: dict[str, Any] = {
            "mauId": mau_id,
            "audioTranscribed": metadata.get("audio_mau") is not None,
            "captions": captions,
            "durableArchive": archive is not None,
            "cache": cache,
            "elapsedMs": round((time.perf_counter() - started) * 1000),
        }
        if kind == "video":
            processed = int(metadata.get("frames_processed") or len(frame_maus))
            skipped = int(metadata.get("frames_skipped") or 0)
            extracted = max(0, processed + skipped)
            reply.update(
                {
                    "fps": fps,
                    "framesExtracted": extracted,
                    "framesProcessed": processed,
                    "framesSkipped": skipped,
                    "coveredThroughSeconds": min(duration_seconds, extracted / fps) if extracted else 0.0,
                }
            )
        return reply
    except Exception:
        if memory is not None:
            try:
                memory.close()
            except Exception:
                pass
        raise


def _result_items(memory_dir: Path, meta: dict[str, Any], rows: list[Any]) -> list[dict[str, Any]]:
    memories = meta.get("memories") or {}
    frames = meta.get("frames") or {}
    items: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        mau_id = str(row.get("id") or "")
        if not mau_id:
            continue
        mapped = frames.get(mau_id)
        record = memories.get(mau_id) or {}
        provenance = record.get("provenance") if isinstance(record.get("provenance"), dict) else {}
        summary = str(row.get("summary") or record.get("summary") or "")[:2000]
        try:
            score = float(row.get("score", 0.0))
        except (TypeError, ValueError):
            score = 0.0
        memory_type = str(record.get("type") or "episode")
        item: dict[str, Any] = {
            "mauId": mau_id,
            # ``ref``/``text``/``metadata`` match the runtime's generic memory
            # hit shape so the existing HttpMemoryProvider can serve this store.
            "ref": mau_id,
            "text": summary,
            "metadata": provenance,
            "modality": _modality(row),
            "type": memory_type,
            "score": score,
            "summary": summary,
            "provenance": provenance,
            "createdAtUnix": record.get("createdAtUnix"),
            "frameIndex": mapped.get("frameIndex") if isinstance(mapped, dict) else None,
            "seconds": mapped.get("seconds") if isinstance(mapped, dict) else None,
        }
        items.append(item)
    return items


def _query_sync(namespace: str, question: str, top_k: int, types: list[str] | None) -> dict[str, Any]:
    _assert_archive_configuration()
    started = time.perf_counter()
    memory_dir, restored = _prepare_cache_for_query(namespace)
    meta = _read_meta(memory_dir)
    _assert_meta_compatible(meta)
    try:
        memory = _open_memory(memory_dir)
    except Exception:
        # Metadata presence catches interrupted writes cheaply. If the upstream
        # store itself is corrupt, discard the cache and retry exactly once from
        # the committed durable generation.
        if restored or not _archive_configured():
            raise
        shutil.rmtree(memory_dir, ignore_errors=True)
        if not _restore_sync(namespace):
            raise
        restored = True
        meta = _read_meta(memory_dir)
        _assert_meta_compatible(meta)
        memory = _open_memory(memory_dir)
    total_candidates = 0
    cache: dict[str, int]
    try:
        # Upstream derives its own strategy and otherwise replaces the caller's
        # top_k with 5/10/20 depending on query type. GNSIS owns the candidate
        # budget, so preserve every other strategy choice while making our
        # requested top_k authoritative.
        original_strategy = memory.query_processor.determine_retrieval_strategy

        def gnsis_strategy(parsed):
            strategy = dict(original_strategy(parsed))
            strategy["top_k"] = top_k
            return strategy

        memory.query_processor.determine_retrieval_strategy = gnsis_strategy
        try:
            result = memory.query(question, top_k=top_k)
        finally:
            memory.query_processor.determine_retrieval_strategy = original_strategy
        total_candidates = int(getattr(result, "total_candidates", len(result.items)) or len(result.items))
        items = _result_items(memory_dir, meta, list(result.items))
        if types:
            wanted = {t for t in types if t in MEMORY_TYPES}
            if wanted:
                items = [item for item in items if item["type"] in wanted]
    finally:
        # The active store must be closed before this namespace is eligible for
        # eviction, so one large/restored namespace cannot pin the cache above
        # its high-water mark indefinitely.
        try:
            memory.close()
        finally:
            _touch_cache_entry(memory_dir)
            cache = _enforce_cache_budget()

    return {
        "items": items,
        # ``results`` mirrors ``items`` so a generic memory-search client that
        # only knows {"results": [...]} works unchanged.
        "results": items,
        "totalCandidates": total_candidates,
        "restoredFromArchive": restored,
        "cache": cache,
        "elapsedMs": round((time.perf_counter() - started) * 1000),
    }


def _recent_sync(namespace: str, limit: int) -> dict[str, Any]:
    memory_dir = _namespace_dir(namespace)
    if not _meta_path(memory_dir).exists():
        if not _restore_sync(namespace) or not _cache_ready(memory_dir, namespace):
            raise HTTPException(status_code=404, detail="namespace memory not found")
    meta = _read_meta(memory_dir)
    _assert_meta_compatible(meta)
    records = sorted(
        (
            {"mauId": mau_id, **(record if isinstance(record, dict) else {})}
            for mau_id, record in (meta.get("memories") or {}).items()
        ),
        key=lambda record: int(record.get("createdAtUnix") or 0),
        reverse=True,
    )
    items = []
    for record in records[:limit]:
        summary = str(record.get("summary") or "")[:2000]
        provenance = record.get("provenance") if isinstance(record.get("provenance"), dict) else {}
        items.append(
            {
                "mauId": record["mauId"],
                "ref": record["mauId"],
                "text": summary,
                "metadata": provenance,
                "type": record.get("type"),
                "summary": summary,
                "provenance": provenance,
                "createdAtUnix": record.get("createdAtUnix"),
            }
        )
    return {"items": items, "results": items, "namespace": namespace}


def _delete_sync(namespace: str) -> None:
    _assert_archive_configuration()
    remote_error: Exception | None = None
    try:
        _delete_archive_sync(namespace)
    except Exception as exc:
        remote_error = exc
    finally:
        # Local derived state must disappear even if object storage is down.
        # The caller receives the remote failure so retention can retry it.
        memory_dir = _namespace_dir(namespace)
        shutil.rmtree(memory_dir, ignore_errors=True)
        shutil.rmtree(memory_dir.with_name(f"{memory_dir.name}.previous"), ignore_errors=True)
    if remote_error is not None:
        raise remote_error


@app.get("/health")
async def health() -> dict[str, Any]:
    _assert_archive_configuration()
    config = _config()
    return {
        "ok": True,
        "models": _models(config),
        "version": simplemem.__version__,
        # Visible here so a deploy can be checked without a shell into the container.
        "libraries": {"transformers": str(getattr(transformers, "__version__", "unknown"))},
        "archive": {
            "configured": _archive_configured(),
            "required": ARCHIVE_REQUIRED,
            "prefix": ARCHIVE_PREFIX,
        },
        "cache": {
            "highWaterBytes": CACHE_HIGH_WATER_BYTES,
            "lowWaterBytes": CACHE_LOW_WATER_BYTES,
        },
        "memoryTypes": sorted(MEMORY_TYPES),
        "embeddingVersion": EMBEDDING_VERSION,
        "maxUploadBytes": MAX_UPLOAD_BYTES,
        "authConfigured": len(INTERNAL_TOKEN) >= 32,
    }


@app.get("/ready")
async def ready(_: None = Depends(_authorize_internal)) -> dict[str, Any]:
    return await health()


@app.put("/namespaces/{namespace}/text")
async def write_text(
    namespace: str,
    body: TextBody,
    _: None = Depends(_authorize_internal),
) -> dict[str, Any]:
    namespace = _safe_namespace(namespace)
    body.type = _check_type(body.type)
    body.provenance = _check_provenance(body.provenance)
    async with operation_lock:
        try:
            return await asyncio.to_thread(_write_text_sync, namespace, body)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"SimpleMem write failed: {type(exc).__name__}") from exc


def _parse_provenance_form(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="provenance must be a JSON object") from exc
    return _check_provenance(value)


def _parse_tags_form(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return [tag.strip() for tag in raw.split(",") if tag.strip()]
    if not isinstance(value, list):
        raise HTTPException(status_code=400, detail="tags must be a JSON array")
    return [str(tag) for tag in value]


async def _media_endpoint(
    kind: str,
    namespace: str,
    file: UploadFile,
    *,
    memory_type: str,
    provenance: str | None,
    tags: str | None,
    session_id: str | None,
    fps: float | None,
    max_frames: int | None,
    duration_seconds: float | None,
) -> dict[str, Any]:
    namespace = _safe_namespace(namespace)
    memory_type = _check_type(memory_type)
    prov = _parse_provenance_form(provenance)
    tag_list = _parse_tags_form(tags)
    suffix = Path(file.filename or f"media.{kind}").suffix or ".bin"
    fd, raw_path = tempfile.mkstemp(prefix=f"gnsis-simplemem-{kind}-", suffix=suffix)
    os.close(fd)
    temp_path = Path(raw_path)
    try:
        async with operation_lock:
            total_bytes = 0
            with temp_path.open("wb") as output:
                while chunk := await file.read(1024 * 1024):
                    total_bytes += len(chunk)
                    if total_bytes > MAX_UPLOAD_BYTES:
                        raise HTTPException(status_code=413, detail="upload exceeds the SimpleMem byte limit")
                    output.write(chunk)
            return await asyncio.to_thread(
                _media_sync,
                namespace,
                kind,
                temp_path,
                memory_type=memory_type,
                provenance=prov,
                session_id=session_id,
                tags=tag_list,
                fps=fps,
                max_frames=max_frames,
                duration_seconds=duration_seconds,
            )
    except HTTPException:
        raise
    except IndexingRefused as exc:
        raise HTTPException(status_code=500, detail=f"SimpleMem indexing refused: {exc}") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"SimpleMem indexing failed: {type(exc).__name__}") from exc
    finally:
        temp_path.unlink(missing_ok=True)


@app.put("/namespaces/{namespace}/image")
async def write_image(
    namespace: str,
    file: UploadFile = File(...),
    type: str = Form("visual_event"),
    provenance: str | None = Form(default=None),
    tags: str | None = Form(default=None),
    session_id: str | None = Form(default=None),
    _: None = Depends(_authorize_internal),
) -> dict[str, Any]:
    return await _media_endpoint(
        "image",
        namespace,
        file,
        memory_type=type,
        provenance=provenance,
        tags=tags,
        session_id=session_id,
        fps=None,
        max_frames=None,
        duration_seconds=None,
    )


@app.put("/namespaces/{namespace}/audio")
async def write_audio(
    namespace: str,
    file: UploadFile = File(...),
    type: str = Form("audio_event"),
    provenance: str | None = Form(default=None),
    tags: str | None = Form(default=None),
    session_id: str | None = Form(default=None),
    _: None = Depends(_authorize_internal),
) -> dict[str, Any]:
    return await _media_endpoint(
        "audio",
        namespace,
        file,
        memory_type=type,
        provenance=provenance,
        tags=tags,
        session_id=session_id,
        fps=None,
        max_frames=None,
        duration_seconds=None,
    )


@app.put("/namespaces/{namespace}/video")
async def write_video(
    namespace: str,
    file: UploadFile = File(...),
    type: str = Form("episode"),
    fps: float = Form(..., gt=0, le=2),
    max_frames: int = Form(..., ge=1, le=200_000),
    duration_seconds: float = Form(..., gt=0),
    provenance: str | None = Form(default=None),
    tags: str | None = Form(default=None),
    session_id: str | None = Form(default=None),
    _: None = Depends(_authorize_internal),
) -> dict[str, Any]:
    return await _media_endpoint(
        "video",
        namespace,
        file,
        memory_type=type,
        provenance=provenance,
        tags=tags,
        session_id=session_id,
        fps=float(fps),
        max_frames=int(max_frames),
        duration_seconds=float(duration_seconds),
    )


@app.post("/namespaces/{namespace}/query")
async def query_namespace(
    namespace: str,
    body: QueryBody,
    _: None = Depends(_authorize_internal),
) -> dict[str, Any]:
    namespace = _safe_namespace(namespace)
    async with operation_lock:
        try:
            top_k = body.top_k if body.limit is None else min(body.top_k, body.limit)
            return await asyncio.to_thread(
                _query_sync, namespace, body.query.strip(), top_k, body.types
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"SimpleMem query failed: {type(exc).__name__}") from exc


@app.get("/namespaces/{namespace}/recent")
async def recent_namespace(
    namespace: str,
    limit: int = Query(default=20, ge=1, le=200),
    _: None = Depends(_authorize_internal),
) -> dict[str, Any]:
    namespace = _safe_namespace(namespace)
    async with operation_lock:
        try:
            return await asyncio.to_thread(_recent_sync, namespace, int(limit))
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"SimpleMem recent failed: {type(exc).__name__}") from exc


@app.delete("/namespaces/{namespace}")
async def delete_namespace(namespace: str, _: None = Depends(_authorize_internal)) -> dict[str, bool]:
    namespace = _safe_namespace(namespace)
    async with operation_lock:
        try:
            await asyncio.to_thread(_delete_sync, namespace)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"SimpleMem delete failed: {type(exc).__name__}") from exc
    return {"ok": True}
