from __future__ import annotations

import hashlib
import sqlite3
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

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return self.get_tuple(config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        for value in self.list(config, filter=filter, before=before, limit=limit):
            yield value

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        return self.put(config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        self.put_writes(config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        self.delete_thread(thread_id)

