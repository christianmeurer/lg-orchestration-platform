# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Christian Meurer — https://github.com/christianmeurer/Lula
"""PostgreSQL checkpoint backend."""

from __future__ import annotations

import base64
import json
import re
from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any, cast

from langchain_core.runnables.config import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_id,
    get_checkpoint_metadata,
)

from lg_orch.backends._base import BaseCheckpointSaver, parse_config

_TABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


def _validate_table_name(name: str) -> str:
    """Validate that *name* is a safe SQL identifier."""
    if not _TABLE_NAME_RE.match(name):
        raise ValueError(f"Invalid table name: {name!r}")
    return name


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


def _dict_row_factory(cursor: Any) -> Any:
    """psycopg3 row factory that returns dicts."""
    from psycopg.rows import dict_row  # type: ignore[import-not-found]

    return dict_row(cursor)


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
            import psycopg_pool  # type: ignore[import-not-found]  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "Install lula with the 'postgres' extra: pip install lula[postgres]"
            ) from exc

        super().__init__()
        self._dsn = dsn
        # MEDIUM FIX 1: Validate table name to prevent SQL injection
        self._table_name = _validate_table_name(table_name)
        self._pool: Any = None
        self._initialized = False

    async def _get_pool(self) -> Any:
        if self._pool is None:
            try:
                from psycopg_pool import AsyncConnectionPool
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
        create_table = _POSTGRES_CREATE_TABLE.replace("lula_checkpoints", self._table_name)
        create_index = _POSTGRES_CREATE_INDEX.replace("lula_checkpoints", self._table_name).replace(
            "idx_lula_ckpt_thread", f"idx_{self._table_name}_thread"
        )
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
        return parse_config(config)

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
            else bytes(metadata_blob_raw)
            if metadata_blob_raw
            else b""
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
            conn.row_factory = _dict_row_factory  # type: ignore[attr-defined,unused-ignore]
            # HIGH FIX 1: Use psycopg3 cursor-based API instead of asyncpg's
            # conn.fetchrow() which does not exist in psycopg3.
            async with conn.cursor() as cur:
                if checkpoint_id is not None:
                    await cur.execute(
                        f"SELECT * FROM {tbl}"
                        f" WHERE thread_id = %s AND checkpoint_ns = %s AND checkpoint_id = %s",
                        (thread_id, checkpoint_ns, checkpoint_id),
                    )
                else:
                    await cur.execute(
                        f"SELECT * FROM {tbl}"
                        f" WHERE thread_id = %s AND checkpoint_ns = %s"
                        f" ORDER BY created_at DESC LIMIT 1",
                        (thread_id, checkpoint_ns),
                    )
                row_tuple = await cur.fetchone()
                if row_tuple is None:
                    return None
                cols = [desc[0] for desc in cur.description or []]
                row = dict(zip(cols, row_tuple, strict=False))
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

        # HIGH FIX 1: Use psycopg3 %s placeholders instead of asyncpg $N
        params: list[Any] = [thread_id, checkpoint_ns]
        where_extra = ""
        if before is not None:
            before_id = get_checkpoint_id(before)
            if before_id is not None:
                params.append(before_id)
                where_extra = " AND checkpoint_id != %s"

        limit_clause = ""
        if limit is not None and limit >= 0:
            params.append(limit)
            limit_clause = " LIMIT %s"

        query = (
            f"SELECT * FROM {tbl} WHERE thread_id = %s AND checkpoint_ns = %s"
            f"{where_extra} ORDER BY created_at DESC{limit_clause}"
        )

        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(query, params)
            cols = [desc[0] for desc in cur.description or []]
            async for row_tuple in cur:
                row = dict(zip(cols, row_tuple, strict=False))
                tup = self._row_to_tuple(row, requested_config=None)
                if filter is not None and any(tup.metadata.get(k) != v for k, v in filter.items()):
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
        metadata_type, metadata_blob = self._dump_typed(get_checkpoint_metadata(config, metadata))

        pool = await self._get_pool()
        tbl = self._table_name
        # HIGH FIX 1: Use psycopg3 %s placeholders instead of asyncpg $N
        async with pool.connection() as conn:
            await conn.execute(
                f"""
                INSERT INTO {tbl}
                    (thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id,
                     checkpoint, checkpoint_type, metadata_type, metadata_blob, pending_writes)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (thread_id, checkpoint_ns, checkpoint_id) DO UPDATE SET
                    parent_checkpoint_id = EXCLUDED.parent_checkpoint_id,
                    checkpoint           = EXCLUDED.checkpoint,
                    checkpoint_type      = EXCLUDED.checkpoint_type,
                    metadata_type        = EXCLUDED.metadata_type,
                    metadata_blob        = EXCLUDED.metadata_blob,
                    pending_writes       = EXCLUDED.pending_writes,
                    created_at           = now()
                """,
                (
                    thread_id,
                    checkpoint_ns,
                    checkpoint_id,
                    parent_checkpoint_id or None,
                    checkpoint_blob,
                    checkpoint_type,
                    metadata_type,
                    metadata_blob,
                    json.dumps([]),
                ),
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

        pool = await self._get_pool()
        tbl = self._table_name
        # HIGH FIX 1: Use psycopg3 %s placeholders instead of asyncpg $N
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT pending_writes FROM {tbl}"
                    f" WHERE thread_id = %s AND checkpoint_ns = %s AND checkpoint_id = %s",
                    (thread_id, checkpoint_ns, checkpoint_id),
                )
                row = await cur.fetchone()
            if row is None:
                return

            existing_writes: list[dict[str, Any]] = row[0] if row[0] is not None else []

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
                f"UPDATE {tbl} SET pending_writes = %s"
                f" WHERE thread_id = %s AND checkpoint_ns = %s AND checkpoint_id = %s",
                (
                    json.dumps(existing_writes),
                    thread_id,
                    checkpoint_ns,
                    checkpoint_id,
                ),
            )
            await conn.commit()

    async def adelete_thread(self, thread_id: str) -> None:
        await self._ensure_schema()
        pool = await self._get_pool()
        tbl = self._table_name
        async with pool.connection() as conn:
            await conn.execute(f"DELETE FROM {tbl} WHERE thread_id = %s", (thread_id,))
            await conn.commit()

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    def close(self) -> None:
        pass


__all__ = ["PostgresCheckpointSaver"]
