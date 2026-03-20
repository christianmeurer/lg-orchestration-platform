from __future__ import annotations

import asyncio
import json
import pathlib
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditEvent:
    ts: str  # ISO-8601 UTC
    subject: str  # JWT sub or "anonymous"
    roles: list[str]
    action: str  # e.g. run.create, run.cancel, run.approve, run.read, run.list, run.search
    resource_id: str | None
    outcome: Literal["ok", "denied", "error"]
    detail: str | None


def to_jsonl(event: AuditEvent) -> str:
    """Return a single JSON line for *event* (no trailing newline)."""
    return json.dumps(
        {
            "ts": event.ts,
            "subject": event.subject,
            "roles": event.roles,
            "action": event.action,
            "resource_id": event.resource_id,
            "outcome": event.outcome,
            "detail": event.detail,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


# ---------------------------------------------------------------------------
# Sink protocol and implementations
# ---------------------------------------------------------------------------


class AuditSink:
    """Protocol for async audit export targets."""

    async def export(self, event: AuditEvent) -> None:  # pragma: no cover
        raise NotImplementedError


class S3AuditSink:
    """Batches events and uploads them to S3 as JSONL blobs.

    *aioboto3* is imported lazily; if not installed the sink is a no-op.
    """

    def __init__(self, bucket: str, prefix: str, region: str) -> None:
        self._bucket = bucket
        self._prefix = prefix.rstrip("/")
        self._region = region
        self._batch: list[AuditEvent] = []
        self._lock = asyncio.Lock()
        self._last_flush: float = 0.0
        self._max_batch = 100
        self._flush_interval = 5.0

    async def export(self, event: AuditEvent) -> None:
        try:
            import aioboto3  # type: ignore[import-untyped]
        except ImportError:
            return

        async with self._lock:
            self._batch.append(event)
            import time

            now = time.monotonic()
            should_flush = (
                len(self._batch) >= self._max_batch
                or (now - self._last_flush) >= self._flush_interval
            )
            if not should_flush:
                return
            batch = list(self._batch)
            self._batch.clear()
            self._last_flush = now

        if not batch:
            return

        date_str = datetime.now(UTC).strftime("%Y-%m-%d")
        key = f"{self._prefix}/{date_str}/{uuid.uuid4().hex}.jsonl"
        body = "\n".join(to_jsonl(e) for e in batch).encode("utf-8")

        try:
            session = aioboto3.Session()
            async with session.client("s3", region_name=self._region) as s3:
                await s3.put_object(Bucket=self._bucket, Key=key, Body=body)
        except Exception:  # noqa: BLE001
            pass


class GCSAuditSink:
    """Batches events and uploads them to GCS as JSONL blobs.

    *google-cloud-storage* is imported lazily; if not installed the sink is a no-op.
    """

    def __init__(self, bucket: str, prefix: str) -> None:
        self._bucket = bucket
        self._prefix = prefix.rstrip("/")
        self._batch: list[AuditEvent] = []
        self._lock = asyncio.Lock()
        self._last_flush: float = 0.0
        self._max_batch = 100
        self._flush_interval = 5.0

    async def export(self, event: AuditEvent) -> None:
        try:
            from google.cloud import storage as gcs  # type: ignore[import-untyped]
        except ImportError:
            return

        async with self._lock:
            self._batch.append(event)
            import time

            now = time.monotonic()
            should_flush = (
                len(self._batch) >= self._max_batch
                or (now - self._last_flush) >= self._flush_interval
            )
            if not should_flush:
                return
            batch = list(self._batch)
            self._batch.clear()
            self._last_flush = now

        if not batch:
            return

        date_str = datetime.now(UTC).strftime("%Y-%m-%d")
        blob_name = f"{self._prefix}/{date_str}/{uuid.uuid4().hex}.jsonl"
        body = "\n".join(to_jsonl(e) for e in batch).encode("utf-8")

        try:
            client = gcs.Client()
            bucket = client.bucket(self._bucket)
            blob = bucket.blob(blob_name)
            blob.upload_from_string(body, content_type="application/x-ndjson")
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditConfig:
    log_path: str = "audit.jsonl"
    sink_type: str | None = None  # "s3" or "gcs" or None
    s3_bucket: str | None = None
    s3_prefix: str = "audit"
    s3_region: str = "us-east-1"
    gcs_bucket: str | None = None
    gcs_prefix: str = "audit"


def build_sink(config: AuditConfig) -> AuditSink | None:
    """Factory: construct the appropriate :class:`AuditSink` from *config*."""
    if config.sink_type is None:
        return None
    if config.sink_type == "s3":
        bucket = config.s3_bucket or ""
        if not bucket:
            return None
        return S3AuditSink(
            bucket=bucket,
            prefix=config.s3_prefix,
            region=config.s3_region,
        )
    if config.sink_type == "gcs":
        bucket = config.gcs_bucket or ""
        if not bucket:
            return None
        return GCSAuditSink(bucket=bucket, prefix=config.gcs_prefix)
    return None


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


class AuditLogger:
    """Thread-safe rolling JSONL audit writer with optional async export sink."""

    def __init__(
        self,
        log_path: pathlib.Path,
        sink: AuditSink | None = None,
    ) -> None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = log_path.open("a", encoding="utf-8", buffering=1)
        self._lock = threading.Lock()
        self._sink = sink

    def log(self, event: AuditEvent) -> None:
        """Write *event* to the JSONL file and export via sink if configured."""
        line = to_jsonl(event) + "\n"
        with self._lock:
            self._file.write(line)
            self._file.flush()

        if self._sink is not None:
            self._export_async(event)

    def _export_async(self, event: AuditEvent) -> None:
        """Schedule sink.export in the running event loop, or in a new thread."""
        sink = self._sink
        if sink is None:
            return
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(sink.export(event))
        except RuntimeError:
            # No running event loop — fire in background thread
            def _run() -> None:
                import asyncio as _asyncio

                _asyncio.run(sink.export(event))

            t = threading.Thread(target=_run, daemon=True)
            t.start()

    def close(self) -> None:
        """Flush and close the underlying file."""
        with self._lock:
            self._file.flush()
            self._file.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def utc_now_iso() -> str:
    """Return current UTC time as ISO-8601 string with Z suffix."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "AuditConfig",
    "AuditEvent",
    "AuditLogger",
    "AuditSink",
    "GCSAuditSink",
    "S3AuditSink",
    "build_sink",
    "to_jsonl",
    "utc_now_iso",
]
