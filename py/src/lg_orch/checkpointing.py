from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from collections.abc import AsyncIterator, Iterator, Sequence
from pathlib import Path
from typing import Any, cast

from langchain_core.runnables.config import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_id,
    get_checkpoint_metadata,
)


class CheckpointBackendError(RuntimeError):
    """Raised when the checkpoint backend fails in a non-recoverable way."""


def resolve_checkpoint_db_path(*, repo_root: Path, db_path: str) -> Path:
    candidate = Path(db_path)
    if candidate.is_absolute():
        return candidate.resolve()
    return (repo_root / candidate).resolve()


def stable_checkpoint_thread_id(*, request: str, thread_prefix: str, provided: str | None) -> str:
    if provided is not None:
        text = provided.strip()
        if text:
            return text
    digest = hashlib.sha256(request.encode("utf-8", errors="replace")).hexdigest()[:16]
    prefix = thread_prefix.strip() or "lg-orch"
    return f"{prefix}-{digest}"


# ---------------------------------------------------------------------------
# SQLite backend (original, unchanged)
# ---------------------------------------------------------------------------


class SqliteCheckpointSaver(BaseCheckpointSaver[Any]):
    def __init__(self, *, db_path: Path) -> None:
        super().__init__()
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _initialize_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS checkpoints (
                    thread_id TEXT NOT NULL,
                    checkpoint_ns TEXT NOT NULL,
                    checkpoint_id TEXT NOT NULL,
                    checkpoint_type TEXT NOT NULL,
                    checkpoint_blob BLOB NOT NULL,
                    metadata_type TEXT NOT NULL,
                    metadata_blob BLOB NOT NULL,
                    parent_checkpoint_id TEXT,
                    created_at_unix INTEGER NOT NULL DEFAULT (unixepoch()),
                    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
                );

                CREATE TABLE IF NOT EXISTS checkpoint_blobs (
                    thread_id TEXT NOT NULL,
                    checkpoint_ns TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    version TEXT NOT NULL,
                    type_tag TEXT NOT NULL,
                    payload BLOB NOT NULL,
                    PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
                );

                CREATE TABLE IF NOT EXISTS checkpoint_writes (
                    thread_id TEXT NOT NULL,
                    checkpoint_ns TEXT NOT NULL,
                    checkpoint_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    idx INTEGER NOT NULL,
                    channel TEXT NOT NULL,
                    type_tag TEXT NOT NULL,
                    payload BLOB NOT NULL,
                    task_path TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
                );

                CREATE INDEX IF NOT EXISTS idx_checkpoints_lookup
                    ON checkpoints(thread_id, checkpoint_ns, checkpoint_id DESC);
                """
            )

    def _dump_typed(self, value: Any) -> tuple[str, bytes]:
        type_tag, payload = self.serde.dumps_typed(value)
        type_text = str(type_tag)
        return type_text, bytes(payload)

    def _load_typed(self, *, type_tag: str, payload: bytes) -> Any:
        return self.serde.loads_typed((type_tag, payload))

    def _parse_config(self, config: RunnableConfig) -> tuple[str, str, str | None]:
        configurable = config.get("configurable", {})
        if not isinstance(configurable, dict):
            raise ValueError("configurable must be a dict")

        thread_id = configurable.get("thread_id")
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValueError("missing configurable.thread_id")

        checkpoint_ns_raw = configurable.get("checkpoint_ns", "")
        checkpoint_ns = str(checkpoint_ns_raw)

        checkpoint_id_raw = configurable.get("checkpoint_id")
        checkpoint_id = str(checkpoint_id_raw) if checkpoint_id_raw is not None else None
        return thread_id.strip(), checkpoint_ns, checkpoint_id

    def _load_channel_values(
        self,
        *,
        conn: sqlite3.Connection,
        thread_id: str,
        checkpoint_ns: str,
        channel_versions: ChannelVersions,
    ) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for channel, version in channel_versions.items():
            row = conn.execute(
                """
                SELECT type_tag, payload
                FROM checkpoint_blobs
                WHERE thread_id = ? AND checkpoint_ns = ? AND channel = ? AND version = ?
                """,
                (thread_id, checkpoint_ns, channel, version),
            ).fetchone()
            if row is None:
                continue
            type_tag = str(row["type_tag"])
            if type_tag == "empty":
                continue
            raw_payload = row["payload"]
            payload = bytes(raw_payload) if not isinstance(raw_payload, bytes) else raw_payload
            values[channel] = self._load_typed(type_tag=type_tag, payload=payload)
        return values

    def _load_pending_writes(
        self,
        *,
        conn: sqlite3.Connection,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str,
    ) -> list[tuple[str, str, Any]]:
        rows = conn.execute(
            """
            SELECT task_id, channel, type_tag, payload
            FROM checkpoint_writes
            WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ?
            ORDER BY idx ASC
            """,
            (thread_id, checkpoint_ns, checkpoint_id),
        ).fetchall()

        writes: list[tuple[str, str, Any]] = []
        for row in rows:
            type_tag = str(row["type_tag"])
            raw_payload = row["payload"]
            payload = bytes(raw_payload) if not isinstance(raw_payload, bytes) else raw_payload
            writes.append(
                (
                    str(row["task_id"]),
                    str(row["channel"]),
                    self._load_typed(type_tag=type_tag, payload=payload),
                )
            )
        return writes

    def _row_to_checkpoint_tuple(
        self,
        *,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        requested_config: RunnableConfig | None,
    ) -> CheckpointTuple:
        thread_id = str(row["thread_id"])
        checkpoint_ns = str(row["checkpoint_ns"])
        checkpoint_id = str(row["checkpoint_id"])

        checkpoint_payload = row["checkpoint_blob"]
        checkpoint_raw = cast(
            Checkpoint,
            self._load_typed(
                type_tag=str(row["checkpoint_type"]),
                payload=(
                    bytes(checkpoint_payload)
                    if not isinstance(checkpoint_payload, bytes)
                    else checkpoint_payload
                ),
            ),
        )

        versions_any = checkpoint_raw.get("channel_versions", {})
        versions: ChannelVersions = versions_any if isinstance(versions_any, dict) else {}
        checkpoint_with_values: Checkpoint = {
            **checkpoint_raw,
            "channel_values": self._load_channel_values(
                conn=conn,
                thread_id=thread_id,
                checkpoint_ns=checkpoint_ns,
                channel_versions=versions,
            ),
        }

        metadata_payload = row["metadata_blob"]
        metadata = cast(
            CheckpointMetadata,
            self._load_typed(
                type_tag=str(row["metadata_type"]),
                payload=(
                    bytes(metadata_payload)
                    if not isinstance(metadata_payload, bytes)
                    else metadata_payload
                ),
            ),
        )

        parent_checkpoint_id_raw = row["parent_checkpoint_id"]
        parent_config: RunnableConfig | None
        if isinstance(parent_checkpoint_id_raw, str) and parent_checkpoint_id_raw:
            parent_config = {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": parent_checkpoint_id_raw,
                }
            }
        else:
            parent_config = None

        out_config: RunnableConfig
        if requested_config is not None:
            out_config = requested_config
        else:
            out_config = {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": checkpoint_id,
                }
            }

        return CheckpointTuple(
            config=out_config,
            checkpoint=checkpoint_with_values,
            metadata=metadata,
            parent_config=parent_config,
            pending_writes=self._load_pending_writes(
                conn=conn,
                thread_id=thread_id,
                checkpoint_ns=checkpoint_ns,
                checkpoint_id=checkpoint_id,
            ),
        )

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread_id, checkpoint_ns, checkpoint_id = self._parse_config(config)
        requested_config: RunnableConfig | None = config if checkpoint_id is not None else None
        with self._connect() as conn:
            if checkpoint_id is not None:
                row = conn.execute(
                    """
                    SELECT *
                    FROM checkpoints
                    WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ?
                    """,
                    (thread_id, checkpoint_ns, checkpoint_id),
                ).fetchone()
                if row is None and checkpoint_ns != "":
                    row = conn.execute(
                        """
                        SELECT *
                        FROM checkpoints
                        WHERE thread_id = ? AND checkpoint_ns = '' AND checkpoint_id = ?
                        """,
                        (thread_id, checkpoint_id),
                    ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT *
                    FROM checkpoints
                    WHERE thread_id = ? AND checkpoint_ns = ?
                    ORDER BY checkpoint_id DESC
                    LIMIT 1
                    """,
                    (thread_id, checkpoint_ns),
                ).fetchone()
                if row is None and checkpoint_ns != "":
                    row = conn.execute(
                        """
                        SELECT *
                        FROM checkpoints
                        WHERE thread_id = ? AND checkpoint_ns = ''
                        ORDER BY checkpoint_id DESC
                        LIMIT 1
                        """,
                        (thread_id,),
                    ).fetchone()
            if row is None:
                return None

            return self._row_to_checkpoint_tuple(
                conn=conn,
                row=row,
                requested_config=requested_config,
            )

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        where_clauses: list[str] = []
        params: list[Any] = []

        if config is not None:
            thread_id, checkpoint_ns, checkpoint_id = self._parse_config(config)
            where_clauses.append("thread_id = ?")
            params.append(thread_id)
            where_clauses.append("checkpoint_ns = ?")
            params.append(checkpoint_ns)
            if checkpoint_id is not None:
                where_clauses.append("checkpoint_id = ?")
                params.append(checkpoint_id)

        if before is not None:
            before_id = get_checkpoint_id(before)
            if before_id is not None:
                where_clauses.append("checkpoint_id < ?")
                params.append(before_id)

        where_sql = ""
        if where_clauses:
            where_sql = f"WHERE {' AND '.join(where_clauses)}"

        limit_sql = ""
        if limit is not None and limit >= 0:
            limit_sql = "LIMIT ?"
            params.append(limit)

        query = f"""
            SELECT *
            FROM checkpoints
            {where_sql}
            ORDER BY checkpoint_id DESC
            {limit_sql}
        """

        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
            for row in rows:
                tuple_value = self._row_to_checkpoint_tuple(
                    conn=conn,
                    row=row,
                    requested_config=None,
                )
                if filter is not None and any(
                    tuple_value.metadata.get(k) != v for k, v in filter.items()
                ):
                    continue
                yield tuple_value

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        thread_id, checkpoint_ns, parent_checkpoint_id = self._parse_config(config)

        checkpoint_copy = checkpoint.copy()
        values_any = checkpoint_copy.get("channel_values", {})
        values = values_any if isinstance(values_any, dict) else {}
        checkpoint_no_values: Checkpoint = {
            **checkpoint_copy,
            "channel_values": {},
        }

        checkpoint_type, checkpoint_blob = self._dump_typed(checkpoint_no_values)
        metadata_type, metadata_blob = self._dump_typed(get_checkpoint_metadata(config, metadata))
        checkpoint_id = str(checkpoint["id"])

        with self._connect() as conn:
            for channel, version in new_versions.items():
                if channel in values:
                    type_tag, payload = self._dump_typed(values[channel])
                else:
                    type_tag, payload = "empty", b""
                conn.execute(
                    """
                    INSERT OR REPLACE INTO checkpoint_blobs
                    (thread_id, checkpoint_ns, channel, version, type_tag, payload)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (thread_id, checkpoint_ns, channel, version, type_tag, payload),
                )

            conn.execute(
                """
                INSERT OR REPLACE INTO checkpoints
                (
                    thread_id,
                    checkpoint_ns,
                    checkpoint_id,
                    checkpoint_type,
                    checkpoint_blob,
                    metadata_type,
                    metadata_blob,
                    parent_checkpoint_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    thread_id,
                    checkpoint_ns,
                    checkpoint_id,
                    checkpoint_type,
                    checkpoint_blob,
                    metadata_type,
                    metadata_blob,
                    parent_checkpoint_id,
                ),
            )

        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        }

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread_id, checkpoint_ns, checkpoint_id = self._parse_config(config)
        if checkpoint_id is None:
            raise ValueError("missing configurable.checkpoint_id")

        with self._connect() as conn:
            for idx, (channel, value) in enumerate(writes):
                write_idx = WRITES_IDX_MAP.get(channel, idx)
                if write_idx >= 0:
                    existing = conn.execute(
                        """
                        SELECT 1
                        FROM checkpoint_writes
                        WHERE thread_id = ?
                          AND checkpoint_ns = ?
                          AND checkpoint_id = ?
                          AND task_id = ?
                          AND idx = ?
                        """,
                        (thread_id, checkpoint_ns, checkpoint_id, task_id, write_idx),
                    ).fetchone()
                    if existing is not None:
                        continue

                type_tag, payload = self._dump_typed(value)
                conn.execute(
                    """
                    INSERT OR REPLACE INTO checkpoint_writes
                    (
                        thread_id,
                        checkpoint_ns,
                        checkpoint_id,
                        task_id,
                        idx,
                        channel,
                        type_tag,
                        payload,
                        task_path
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        thread_id,
                        checkpoint_ns,
                        checkpoint_id,
                        task_id,
                        write_idx,
                        channel,
                        type_tag,
                        payload,
                        task_path,
                    ),
                )

    def delete_thread(self, thread_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM checkpoints WHERE thread_id = ?", (thread_id,))
            conn.execute("DELETE FROM checkpoint_blobs WHERE thread_id = ?", (thread_id,))
            conn.execute("DELETE FROM checkpoint_writes WHERE thread_id = ?", (thread_id,))

    # Offloaded to thread to avoid blocking the asyncio event loop.
    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return await asyncio.to_thread(self.get_tuple, config)

    # Offloaded to thread to avoid blocking the asyncio event loop.
    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        results = await asyncio.to_thread(
            lambda: list(self.list(config, filter=filter, before=before, limit=limit))
        )
        for value in results:
            yield value

    # Offloaded to thread to avoid blocking the asyncio event loop.
    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        return await asyncio.to_thread(self.put, config, checkpoint, metadata, new_versions)

    # Offloaded to thread to avoid blocking the asyncio event loop.
    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        await asyncio.to_thread(self.put_writes, config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        await asyncio.to_thread(self.delete_thread, thread_id)


# ---------------------------------------------------------------------------
# Redis backend
# ---------------------------------------------------------------------------


def _try_import_msgpack() -> Any:
    try:
        import msgpack  # type: ignore[import-untyped]

        return msgpack
    except ImportError:
        return None


def _serialize(data: dict[str, Any]) -> bytes:
    msgpack = _try_import_msgpack()
    if msgpack is not None:
        return cast(bytes, msgpack.packb(data, use_bin_type=True))
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def _deserialize(raw: bytes) -> dict[str, Any]:
    msgpack = _try_import_msgpack()
    if msgpack is not None:
        result = msgpack.unpackb(raw, raw=False)
        if not isinstance(result, dict):
            raise CheckpointBackendError("Deserialized Redis value is not a dict")
        return cast(dict[str, Any], result)
    decoded = json.loads(raw.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise CheckpointBackendError("Deserialized Redis value is not a dict")
    return cast(dict[str, Any], decoded)


class RedisCheckpointSaver(BaseCheckpointSaver[Any]):
    """Async-native checkpoint saver backed by Redis.

    Requires the ``redis`` optional dependency group::

        pip install lg-orch[redis]

    Sync interface methods (``get_tuple``, ``list``, ``put``, ``put_writes``)
    raise ``NotImplementedError``; use the async variants (``aget_tuple``,
    ``alist``, ``aput``, ``aput_writes``) with an async LangGraph runtime.
    """

    def __init__(
        self,
        redis_url: str,
        key_prefix: str = "lula:ckpt:",
        ttl_seconds: int = 86400,
    ) -> None:
        try:
            import redis.asyncio as aioredis  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ImportError(
                "Install lula with the 'redis' extra: pip install lula[redis]"
            ) from exc

        super().__init__()
        self._redis_url = redis_url
        self._key_prefix = key_prefix
        self._ttl_seconds = ttl_seconds
        self._client: Any = aioredis.from_url(redis_url, decode_responses=False)

    # ------------------------------------------------------------------
    # Key helpers
    # ------------------------------------------------------------------

    def _ckpt_key(self, thread_id: str, checkpoint_ns: str, checkpoint_id: str) -> str:
        return f"{self._key_prefix}{thread_id}:{checkpoint_ns}:{checkpoint_id}"

    def _blobs_key(
        self, thread_id: str, checkpoint_ns: str, channel: str, version: str
    ) -> str:
        return f"{self._key_prefix}blobs:{thread_id}:{checkpoint_ns}:{channel}:{version}"

    def _writes_key(
        self, thread_id: str, checkpoint_ns: str, checkpoint_id: str
    ) -> str:
        return f"{self._key_prefix}writes:{thread_id}:{checkpoint_ns}:{checkpoint_id}"

    def _idx_key(self, thread_id: str, checkpoint_ns: str) -> str:
        return f"{self._key_prefix}idx:{thread_id}:{checkpoint_ns}"

    # ------------------------------------------------------------------
    # Config parsing (mirrors SQLite implementation)
    # ------------------------------------------------------------------

    def _parse_config(self, config: RunnableConfig) -> tuple[str, str, str | None]:
        configurable = config.get("configurable", {})
        if not isinstance(configurable, dict):
            raise ValueError("configurable must be a dict")
        thread_id = configurable.get("thread_id")
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValueError("missing configurable.thread_id")
        checkpoint_ns = str(configurable.get("checkpoint_ns", ""))
        checkpoint_id_raw = configurable.get("checkpoint_id")
        checkpoint_id = str(checkpoint_id_raw) if checkpoint_id_raw is not None else None
        return thread_id.strip(), checkpoint_ns, checkpoint_id

    # ------------------------------------------------------------------
    # Sync stubs — not supported; Redis requires async usage
    # ------------------------------------------------------------------

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        raise NotImplementedError(
            "RedisCheckpointSaver does not support synchronous access. "
            "Use aget_tuple() with an async runtime."
        )

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        raise NotImplementedError(
            "RedisCheckpointSaver does not support synchronous access. "
            "Use alist() with an async runtime."
        )

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        raise NotImplementedError(
            "RedisCheckpointSaver does not support synchronous access. "
            "Use aput() with an async runtime."
        )

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        raise NotImplementedError(
            "RedisCheckpointSaver does not support synchronous access. "
            "Use aput_writes() with an async runtime."
        )

    # ------------------------------------------------------------------
    # Async implementation
    # ------------------------------------------------------------------

    def _dump_typed(self, value: Any) -> tuple[str, bytes]:
        type_tag, payload = self.serde.dumps_typed(value)
        return str(type_tag), bytes(payload)

    def _load_typed(self, *, type_tag: str, payload: bytes) -> Any:
        return self.serde.loads_typed((type_tag, payload))

    async def _expire_keys(self, *keys: str) -> None:
        for key in keys:
            await self._client.expire(key, self._ttl_seconds)

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        try:
            thread_id, checkpoint_ns, checkpoint_id = self._parse_config(config)
            requested_config: RunnableConfig | None = config if checkpoint_id is not None else None

            if checkpoint_id is None:
                # Fetch latest from sorted set (highest score = most recent)
                idx_key = self._idx_key(thread_id, checkpoint_ns)
                members = await self._client.zrevrange(idx_key, 0, 0)
                if not members:
                    return None
                checkpoint_id = members[0].decode("utf-8") if isinstance(members[0], bytes) else str(members[0])

            ckpt_key = self._ckpt_key(thread_id, checkpoint_ns, checkpoint_id)
            raw = await self._client.get(ckpt_key)
            if raw is None:
                return None

            data = _deserialize(cast(bytes, raw))
            return self._data_to_tuple(
                data=data,
                thread_id=thread_id,
                checkpoint_ns=checkpoint_ns,
                checkpoint_id=checkpoint_id,
                requested_config=requested_config,
            )
        except CheckpointBackendError:
            raise
        except Exception as exc:
            raise CheckpointBackendError(f"Redis aget_tuple failed: {exc}") from exc

    def _data_to_tuple(
        self,
        *,
        data: dict[str, Any],
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str,
        requested_config: RunnableConfig | None,
    ) -> CheckpointTuple:
        checkpoint_type = str(data["checkpoint_type"])
        checkpoint_blob = data["checkpoint_blob"]
        checkpoint_payload = (
            checkpoint_blob
            if isinstance(checkpoint_blob, bytes)
            else bytes(checkpoint_blob)
        )
        checkpoint = cast(
            Checkpoint,
            self._load_typed(type_tag=checkpoint_type, payload=checkpoint_payload),
        )

        metadata_type = str(data["metadata_type"])
        metadata_blob = data["metadata_blob"]
        metadata_payload = (
            metadata_blob if isinstance(metadata_blob, bytes) else bytes(metadata_blob)
        )
        metadata = cast(
            CheckpointMetadata,
            self._load_typed(type_tag=metadata_type, payload=metadata_payload),
        )

        parent_checkpoint_id_raw = data.get("parent_checkpoint_id")
        parent_config: RunnableConfig | None = None
        if isinstance(parent_checkpoint_id_raw, str) and parent_checkpoint_id_raw:
            parent_config = {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": parent_checkpoint_id_raw,
                }
            }

        out_config: RunnableConfig = requested_config or {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        }

        # Reconstruct pending_writes from stored list
        raw_writes_list = data.get("pending_writes", [])
        pending_writes: list[tuple[str, str, Any]] = []
        if isinstance(raw_writes_list, list):
            for entry in raw_writes_list:
                if not isinstance(entry, dict):
                    continue
                w_type = str(entry.get("type_tag", ""))
                w_payload_raw = entry.get("payload", b"")
                w_payload = (
                    w_payload_raw
                    if isinstance(w_payload_raw, bytes)
                    else bytes(w_payload_raw)
                )
                pending_writes.append(
                    (
                        str(entry.get("task_id", "")),
                        str(entry.get("channel", "")),
                        self._load_typed(type_tag=w_type, payload=w_payload),
                    )
                )

        return CheckpointTuple(
            config=out_config,
            checkpoint=checkpoint,
            metadata=metadata,
            parent_config=parent_config,
            pending_writes=pending_writes,
        )

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        try:
            if config is None:
                return

            thread_id, checkpoint_ns, _ = self._parse_config(config)
            idx_key = self._idx_key(thread_id, checkpoint_ns)

            before_score: float = float("+inf")
            if before is not None:
                before_id = get_checkpoint_id(before)
                if before_id is not None:
                    # Get score of that checkpoint_id
                    score_raw = await self._client.zscore(idx_key, before_id)
                    if score_raw is not None:
                        before_score = float(score_raw)

            count = limit if limit is not None and limit >= 0 else -1
            members: list[Any] = await self._client.zrevrangebyscore(
                idx_key,
                before_score,
                "-inf",
                start=0,
                num=count,
                withscores=False,
            )

            yielded = 0
            for member in members:
                if limit is not None and yielded >= limit:
                    break
                ckpt_id = member.decode("utf-8") if isinstance(member, bytes) else str(member)
                ckpt_key = self._ckpt_key(thread_id, checkpoint_ns, ckpt_id)
                raw = await self._client.get(ckpt_key)
                if raw is None:
                    continue
                data = _deserialize(cast(bytes, raw))
                tup = self._data_to_tuple(
                    data=data,
                    thread_id=thread_id,
                    checkpoint_ns=checkpoint_ns,
                    checkpoint_id=ckpt_id,
                    requested_config=None,
                )
                if filter is not None and any(
                    tup.metadata.get(k) != v for k, v in filter.items()
                ):
                    continue
                yield tup
                yielded += 1
        except CheckpointBackendError:
            raise
        except Exception as exc:
            raise CheckpointBackendError(f"Redis alist failed: {exc}") from exc

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        thread_id, checkpoint_ns, parent_checkpoint_id = self._parse_config(config)
        checkpoint_id = str(checkpoint["id"])

        checkpoint_type, checkpoint_blob = self._dump_typed(checkpoint)
        metadata_type, metadata_blob = self._dump_typed(
            get_checkpoint_metadata(config, metadata)
        )

        data: dict[str, Any] = {
            "checkpoint_type": checkpoint_type,
            "checkpoint_blob": checkpoint_blob,
            "metadata_type": metadata_type,
            "metadata_blob": metadata_blob,
            "parent_checkpoint_id": parent_checkpoint_id or "",
            "pending_writes": [],
        }

        ckpt_key = self._ckpt_key(thread_id, checkpoint_ns, checkpoint_id)
        idx_key = self._idx_key(thread_id, checkpoint_ns)
        score = time.time()

        await self._client.set(ckpt_key, _serialize(data))
        await self._client.zadd(idx_key, {checkpoint_id: score})
        await self._expire_keys(ckpt_key, idx_key)

        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        }

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread_id, checkpoint_ns, checkpoint_id = self._parse_config(config)
        if checkpoint_id is None:
            raise ValueError("missing configurable.checkpoint_id")

        ckpt_key = self._ckpt_key(thread_id, checkpoint_ns, checkpoint_id)
        raw = await self._client.get(ckpt_key)
        if raw is None:
            return

        data = _deserialize(cast(bytes, raw))
        existing_writes: list[dict[str, Any]] = data.get("pending_writes", [])
        if not isinstance(existing_writes, list):
            existing_writes = []

        existing_keys: set[tuple[str, int]] = set()
        for w in existing_writes:
            if isinstance(w, dict):
                existing_keys.add((str(w.get("task_id", "")), int(w.get("idx", -999))))

        for idx, (channel, value) in enumerate(writes):
            write_idx = WRITES_IDX_MAP.get(channel, idx)
            if write_idx >= 0 and (task_id, write_idx) in existing_keys:
                continue
            type_tag, payload = self._dump_typed(value)
            existing_writes.append(
                {
                    "task_id": task_id,
                    "idx": write_idx,
                    "channel": channel,
                    "type_tag": type_tag,
                    "payload": payload,
                    "task_path": task_path,
                }
            )

        data["pending_writes"] = existing_writes
        await self._client.set(ckpt_key, _serialize(data))
        await self._expire_keys(ckpt_key)

    async def adelete_thread(self, thread_id: str) -> None:
        # Scan all keys with the thread prefix and delete them
        pattern = f"{self._key_prefix}{thread_id}:*"
        cursor = 0
        while True:
            cursor, keys = await self._client.scan(cursor, match=pattern, count=100)
            if keys:
                await self._client.delete(*keys)
            if cursor == 0:
                break

    async def aclose(self) -> None:
        await self._client.aclose()

    def close(self) -> None:
        # Synchronous close is not well-defined for an async Redis client;
        # call aclose() from an async context instead.
        pass


# ---------------------------------------------------------------------------
# Postgres backend
# ---------------------------------------------------------------------------

_POSTGRES_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS lula_checkpoints (
    thread_id             TEXT NOT NULL,
    checkpoint_ns         TEXT NOT NULL DEFAULT '',
    checkpoint_id         TEXT NOT NULL,
    parent_checkpoint_id  TEXT,
    checkpoint            BYTEA NOT NULL,
    checkpoint_type       TEXT NOT NULL DEFAULT '',
    metadata              JSONB,
    metadata_type         TEXT NOT NULL DEFAULT '',
    metadata_blob         BYTEA,
    pending_writes        JSONB,
    created_at            TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);
"""

_POSTGRES_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_lula_ckpt_thread
    ON lula_checkpoints(thread_id, checkpoint_ns, created_at DESC);
"""


class PostgresCheckpointSaver(BaseCheckpointSaver[Any]):
    """Async checkpoint saver backed by PostgreSQL (psycopg v3).

    Requires the ``postgres`` optional dependency group::

        pip install lg-orch[postgres]

    Sync interface methods raise ``NotImplementedError``; use the async
    variants with an async LangGraph runtime.

    The connection pool is created lazily on first use. Call ``aclose()`` to
    shut down the pool gracefully.
    """

    def __init__(
        self,
        dsn: str,
        table_name: str = "lula_checkpoints",
    ) -> None:
        try:
            import psycopg_pool  # type: ignore[import-untyped]  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "Install lula with the 'postgres' extra: pip install lula[postgres]"
            ) from exc

        super().__init__()
        self._dsn = dsn
        self._table_name = table_name
        self._pool: Any = None
        self._initialized = False

    async def _get_pool(self) -> Any:
        if self._pool is None:
            try:
                from psycopg_pool import AsyncConnectionPool  # type: ignore[import-untyped]
            except ImportError as exc:
                raise ImportError(
                    "Install lula with the 'postgres' extra: pip install lula[postgres]"
                ) from exc
            self._pool = AsyncConnectionPool(self._dsn, open=False)
            await self._pool.open()
        return self._pool

    async def _ensure_schema(self) -> None:
        if self._initialized:
            return
        pool = await self._get_pool()
        # Use table_name safely — it is a fixed string from the constructor,
        # never user-supplied data at runtime.
        create_table = _POSTGRES_CREATE_TABLE.replace(
            "lula_checkpoints", self._table_name
        )
        create_index = _POSTGRES_CREATE_INDEX.replace(
            "lula_checkpoints", self._table_name
        ).replace("idx_lula_ckpt_thread", f"idx_{self._table_name}_thread")
        async with pool.connection() as conn:
            await conn.execute(create_table)
            await conn.execute(create_index)
            await conn.commit()
        self._initialized = True

    def _dump_typed(self, value: Any) -> tuple[str, bytes]:
        type_tag, payload = self.serde.dumps_typed(value)
        return str(type_tag), bytes(payload)

    def _load_typed(self, *, type_tag: str, payload: bytes) -> Any:
        return self.serde.loads_typed((type_tag, payload))

    def _parse_config(self, config: RunnableConfig) -> tuple[str, str, str | None]:
        configurable = config.get("configurable", {})
        if not isinstance(configurable, dict):
            raise ValueError("configurable must be a dict")
        thread_id = configurable.get("thread_id")
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValueError("missing configurable.thread_id")
        checkpoint_ns = str(configurable.get("checkpoint_ns", ""))
        checkpoint_id_raw = configurable.get("checkpoint_id")
        checkpoint_id = str(checkpoint_id_raw) if checkpoint_id_raw is not None else None
        return thread_id.strip(), checkpoint_ns, checkpoint_id

    def _row_to_tuple(
        self,
        row: Any,
        *,
        requested_config: RunnableConfig | None,
    ) -> CheckpointTuple:
        thread_id = str(row["thread_id"])
        checkpoint_ns = str(row["checkpoint_ns"])
        checkpoint_id = str(row["checkpoint_id"])
        checkpoint_type = str(row["checkpoint_type"])
        checkpoint_raw_blob = row["checkpoint"]
        checkpoint_payload = (
            checkpoint_raw_blob
            if isinstance(checkpoint_raw_blob, (bytes, memoryview))
            else bytes(checkpoint_raw_blob)
        )
        if isinstance(checkpoint_payload, memoryview):
            checkpoint_payload = bytes(checkpoint_payload)

        checkpoint = cast(
            Checkpoint,
            self._load_typed(type_tag=checkpoint_type, payload=checkpoint_payload),
        )

        metadata_type = str(row["metadata_type"])
        metadata_blob_raw = row["metadata_blob"]
        metadata_payload = (
            metadata_blob_raw
            if isinstance(metadata_blob_raw, (bytes, memoryview))
            else bytes(metadata_blob_raw) if metadata_blob_raw else b""
        )
        if isinstance(metadata_payload, memoryview):
            metadata_payload = bytes(metadata_payload)

        metadata = cast(
            CheckpointMetadata,
            self._load_typed(type_tag=metadata_type, payload=metadata_payload)
            if metadata_payload
            else {},
        )

        parent_checkpoint_id_raw = row["parent_checkpoint_id"]
        parent_config: RunnableConfig | None = None
        if isinstance(parent_checkpoint_id_raw, str) and parent_checkpoint_id_raw:
            parent_config = {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": parent_checkpoint_id_raw,
                }
            }

        out_config: RunnableConfig = requested_config or {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        }

        pending_writes_raw = row["pending_writes"]
        pending_writes: list[tuple[str, str, Any]] = []
        if isinstance(pending_writes_raw, list):
            for entry in pending_writes_raw:
                if not isinstance(entry, dict):
                    continue
                w_type = str(entry.get("type_tag", ""))
                w_payload_raw = entry.get("payload", "")
                if isinstance(w_payload_raw, str):
                    import base64
                    w_payload = base64.b64decode(w_payload_raw)
                elif isinstance(w_payload_raw, (bytes, memoryview)):
                    w_payload = bytes(w_payload_raw)
                else:
                    w_payload = b""
                pending_writes.append(
                    (
                        str(entry.get("task_id", "")),
                        str(entry.get("channel", "")),
                        self._load_typed(type_tag=w_type, payload=w_payload),
                    )
                )

        return CheckpointTuple(
            config=out_config,
            checkpoint=checkpoint,
            metadata=metadata,
            parent_config=parent_config,
            pending_writes=pending_writes,
        )

    # ------------------------------------------------------------------
    # Sync stubs
    # ------------------------------------------------------------------

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        raise NotImplementedError(
            "PostgresCheckpointSaver does not support synchronous access. "
            "Use aget_tuple() with an async runtime."
        )

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        raise NotImplementedError(
            "PostgresCheckpointSaver does not support synchronous access. "
            "Use alist() with an async runtime."
        )

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        raise NotImplementedError(
            "PostgresCheckpointSaver does not support synchronous access. "
            "Use aput() with an async runtime."
        )

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        raise NotImplementedError(
            "PostgresCheckpointSaver does not support synchronous access. "
            "Use aput_writes() with an async runtime."
        )

    # ------------------------------------------------------------------
    # Async implementation
    # ------------------------------------------------------------------

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        await self._ensure_schema()
        thread_id, checkpoint_ns, checkpoint_id = self._parse_config(config)
        requested_config: RunnableConfig | None = config if checkpoint_id is not None else None

        pool = await self._get_pool()
        tbl = self._table_name
        async with pool.connection() as conn:
            conn.row_factory = _dict_row_factory  # type: ignore[attr-defined]
            if checkpoint_id is not None:
                row = await conn.fetchrow(
                    f"SELECT * FROM {tbl} WHERE thread_id = $1 AND checkpoint_ns = $2 AND checkpoint_id = $3",
                    thread_id, checkpoint_ns, checkpoint_id,
                )
            else:
                row = await conn.fetchrow(
                    f"SELECT * FROM {tbl} WHERE thread_id = $1 AND checkpoint_ns = $2 ORDER BY created_at DESC LIMIT 1",
                    thread_id, checkpoint_ns,
                )
            if row is None:
                return None
            return self._row_to_tuple(row, requested_config=requested_config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        await self._ensure_schema()
        if config is None:
            return

        thread_id, checkpoint_ns, _ = self._parse_config(config)
        pool = await self._get_pool()
        tbl = self._table_name

        params: list[Any] = [thread_id, checkpoint_ns]
        where_extra = ""
        if before is not None:
            before_id = get_checkpoint_id(before)
            if before_id is not None:
                params.append(before_id)
                where_extra = f" AND checkpoint_id != ${len(params)}"

        limit_clause = ""
        if limit is not None and limit >= 0:
            params.append(limit)
            limit_clause = f" LIMIT ${len(params)}"

        query = (
            f"SELECT * FROM {tbl} WHERE thread_id = $1 AND checkpoint_ns = $2"
            f"{where_extra} ORDER BY created_at DESC{limit_clause}"
        )

        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(query, params)
                cols = [desc[0] for desc in cur.description or []]
                async for row_tuple in cur:
                    row = dict(zip(cols, row_tuple))
                    tup = self._row_to_tuple(row, requested_config=None)
                    if filter is not None and any(
                        tup.metadata.get(k) != v for k, v in filter.items()
                    ):
                        continue
                    yield tup

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        await self._ensure_schema()
        thread_id, checkpoint_ns, parent_checkpoint_id = self._parse_config(config)
        checkpoint_id = str(checkpoint["id"])

        checkpoint_type, checkpoint_blob = self._dump_typed(checkpoint)
        metadata_type, metadata_blob = self._dump_typed(
            get_checkpoint_metadata(config, metadata)
        )

        pool = await self._get_pool()
        tbl = self._table_name
        async with pool.connection() as conn:
            await conn.execute(
                f"""
                INSERT INTO {tbl}
                    (thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id,
                     checkpoint, checkpoint_type, metadata_type, metadata_blob, pending_writes)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (thread_id, checkpoint_ns, checkpoint_id) DO UPDATE SET
                    parent_checkpoint_id = EXCLUDED.parent_checkpoint_id,
                    checkpoint           = EXCLUDED.checkpoint,
                    checkpoint_type      = EXCLUDED.checkpoint_type,
                    metadata_type        = EXCLUDED.metadata_type,
                    metadata_blob        = EXCLUDED.metadata_blob,
                    pending_writes       = EXCLUDED.pending_writes,
                    created_at           = now()
                """,
                thread_id,
                checkpoint_ns,
                checkpoint_id,
                parent_checkpoint_id or None,
                checkpoint_blob,
                checkpoint_type,
                metadata_type,
                metadata_blob,
                json.dumps([]),
            )
            await conn.commit()

        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        }

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        await self._ensure_schema()
        thread_id, checkpoint_ns, checkpoint_id = self._parse_config(config)
        if checkpoint_id is None:
            raise ValueError("missing configurable.checkpoint_id")

        import base64

        pool = await self._get_pool()
        tbl = self._table_name
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT pending_writes FROM {tbl} WHERE thread_id = $1 AND checkpoint_ns = $2 AND checkpoint_id = $3",
                    (thread_id, checkpoint_ns, checkpoint_id),
                )
                row = await cur.fetchone()
            if row is None:
                return

            existing_writes: list[dict[str, Any]] = row[0] if row[0] is not None else []
            if not isinstance(existing_writes, list):
                existing_writes = []

            existing_keys: set[tuple[str, int]] = {
                (str(w.get("task_id", "")), int(w.get("idx", -999)))
                for w in existing_writes
                if isinstance(w, dict)
            }

            for idx, (channel, value) in enumerate(writes):
                write_idx = WRITES_IDX_MAP.get(channel, idx)
                if write_idx >= 0 and (task_id, write_idx) in existing_keys:
                    continue
                type_tag, payload = self._dump_typed(value)
                existing_writes.append(
                    {
                        "task_id": task_id,
                        "idx": write_idx,
                        "channel": channel,
                        "type_tag": type_tag,
                        # Store bytes as base64 string for JSONB compatibility
                        "payload": base64.b64encode(payload).decode("ascii"),
                        "task_path": task_path,
                    }
                )

            await conn.execute(
                f"UPDATE {tbl} SET pending_writes = $1 WHERE thread_id = $2 AND checkpoint_ns = $3 AND checkpoint_id = $4",
                json.dumps(existing_writes),
                thread_id,
                checkpoint_ns,
                checkpoint_id,
            )
            await conn.commit()

    async def adelete_thread(self, thread_id: str) -> None:
        await self._ensure_schema()
        pool = await self._get_pool()
        tbl = self._table_name
        async with pool.connection() as conn:
            await conn.execute(f"DELETE FROM {tbl} WHERE thread_id = $1", thread_id)
            await conn.commit()

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    def close(self) -> None:
        pass


def _dict_row_factory(cursor: Any) -> Any:
    """psycopg3 row factory that returns dicts."""
    from psycopg.rows import dict_row  # type: ignore[import-untyped]

    return dict_row(cursor)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_checkpoint_saver(backend: str, **kwargs: Any) -> BaseCheckpointSaver[Any]:
    """Create a checkpoint saver for the given backend.

    Parameters
    ----------
    backend:
        One of ``"sqlite"``, ``"redis"``, or ``"postgres"``.
    **kwargs:
        Forwarded to the appropriate constructor:

        - ``sqlite``: ``db_path: Path``
        - ``redis``: ``redis_url: str``, ``key_prefix: str``, ``ttl_seconds: int``
        - ``postgres``: ``dsn: str``, ``table_name: str``
    """
    if backend == "sqlite":
        return SqliteCheckpointSaver(**kwargs)
    if backend == "redis":
        return RedisCheckpointSaver(**kwargs)
    if backend == "postgres":
        return PostgresCheckpointSaver(**kwargs)
    raise ValueError(
        f"Unknown checkpoint backend: {backend!r}. Expected one of: 'sqlite', 'redis', 'postgres'."
    )
