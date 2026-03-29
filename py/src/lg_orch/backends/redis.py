# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Christian Meurer — https://github.com/christianmeurer/Lula
"""Redis checkpoint backend."""

from __future__ import annotations

import json
import time
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

from lg_orch.backends._base import BaseCheckpointSaver, CheckpointBackendError, parse_config


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

    # Timeout (seconds) for socket connect and per-command operations.
    # Prevents indefinite hangs when the Redis/Valkey instance is unreachable.
    _DEFAULT_SOCKET_CONNECT_TIMEOUT: float = 5.0
    _DEFAULT_SOCKET_TIMEOUT: float = 10.0

    def __init__(
        self,
        redis_url: str,
        key_prefix: str = "lula:ckpt:",
        ttl_seconds: int = 86400,
        socket_connect_timeout: float | None = None,
        socket_timeout: float | None = None,
    ) -> None:
        try:
            import redis
            import redis.asyncio as aioredis
        except ImportError as exc:
            raise ImportError(
                "Install lula with the 'redis' extra: pip install lula[redis]"
            ) from exc

        super().__init__()
        self._redis_url = redis_url
        self._key_prefix = key_prefix
        self._ttl_seconds = ttl_seconds

        _conn_timeout = socket_connect_timeout if socket_connect_timeout is not None else self._DEFAULT_SOCKET_CONNECT_TIMEOUT
        _sock_timeout = socket_timeout if socket_timeout is not None else self._DEFAULT_SOCKET_TIMEOUT

        # Async client for aget_tuple, aput, etc.
        self._client: Any = aioredis.from_url(
            redis_url,
            decode_responses=False,
            socket_connect_timeout=_conn_timeout,
            socket_timeout=_sock_timeout,
        )
        # Sync client for get_tuple, put, etc. (avoids asyncio.run deadlocks)
        self._sync_client: Any = redis.from_url(
            redis_url,
            decode_responses=False,
            socket_connect_timeout=_conn_timeout,
            socket_timeout=_sock_timeout,
        )

    # ------------------------------------------------------------------
    # Key helpers
    # ------------------------------------------------------------------

    def _ckpt_key(self, thread_id: str, checkpoint_ns: str, checkpoint_id: str) -> str:
        return f"{self._key_prefix}{thread_id}:{checkpoint_ns}:{checkpoint_id}"

    def _blobs_key(self, thread_id: str, checkpoint_ns: str, channel: str, version: str) -> str:
        return f"{self._key_prefix}blobs:{thread_id}:{checkpoint_ns}:{channel}:{version}"

    def _writes_key(self, thread_id: str, checkpoint_ns: str, checkpoint_id: str) -> str:
        return f"{self._key_prefix}writes:{thread_id}:{checkpoint_ns}:{checkpoint_id}"

    def _idx_key(self, thread_id: str, checkpoint_ns: str) -> str:
        return f"{self._key_prefix}idx:{thread_id}:{checkpoint_ns}"

    # ------------------------------------------------------------------
    # Config parsing
    # ------------------------------------------------------------------

    def _parse_config(self, config: RunnableConfig) -> tuple[str, str, str | None]:
        return parse_config(config)

    # ------------------------------------------------------------------
    # Sync implementation
    # ------------------------------------------------------------------

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        try:
            thread_id, checkpoint_ns, checkpoint_id = self._parse_config(config)
            requested_config: RunnableConfig | None = config if checkpoint_id is not None else None

            if checkpoint_id is None:
                idx_key = self._idx_key(thread_id, checkpoint_ns)
                members = self._sync_client.zrevrange(idx_key, 0, 0)
                if not members:
                    return None
                checkpoint_id = (
                    members[0].decode("utf-8") if isinstance(members[0], bytes) else str(members[0])
                )

            ckpt_key = self._ckpt_key(thread_id, checkpoint_ns, checkpoint_id)
            raw = self._sync_client.get(ckpt_key)
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
            raise CheckpointBackendError(f"Redis get_tuple failed: {exc}") from exc

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        try:
            if config is None:
                return

            thread_id, checkpoint_ns, _ = self._parse_config(config)
            idx_key = self._idx_key(thread_id, checkpoint_ns)

            before_score: float = float("+inf")
            if before is not None:
                before_id = get_checkpoint_id(before)
                if before_id is not None:
                    score_raw = self._sync_client.zscore(idx_key, before_id)
                    if score_raw is not None:
                        before_score = float(score_raw)

            count = limit if limit is not None and limit >= 0 else -1
            members = self._sync_client.zrevrangebyscore(
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
                raw = self._sync_client.get(ckpt_key)
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
                if filter is not None and any(tup.metadata.get(k) != v for k, v in filter.items()):
                    continue
                yield tup
                yielded += 1
        except CheckpointBackendError:
            raise
        except Exception as exc:
            raise CheckpointBackendError(f"Redis list failed: {exc}") from exc

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        thread_id, checkpoint_ns, parent_checkpoint_id = self._parse_config(config)
        checkpoint_id = str(checkpoint["id"])

        checkpoint_type, checkpoint_blob = self._dump_typed(checkpoint)
        metadata_type, metadata_blob = self._dump_typed(get_checkpoint_metadata(config, metadata))

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

        self._sync_client.set(ckpt_key, _serialize(data))
        self._sync_client.zadd(idx_key, {checkpoint_id: score})
        self._expire_keys_sync(ckpt_key, idx_key)

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

        ckpt_key = self._ckpt_key(thread_id, checkpoint_ns, checkpoint_id)
        raw = self._sync_client.get(ckpt_key)
        if raw is None:
            return

        data = _deserialize(cast(bytes, raw))
        existing_writes: list[dict[str, Any]] = data.get("pending_writes", [])

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
        self._sync_client.set(ckpt_key, _serialize(data))
        self._expire_keys_sync(ckpt_key)

    # ------------------------------------------------------------------
    # Async implementation
    # ------------------------------------------------------------------

    def _dump_typed(self, value: Any) -> tuple[str, bytes]:
        type_tag, payload = self.serde.dumps_typed(value)
        return str(type_tag), bytes(payload)

    def _load_typed(self, *, type_tag: str, payload: bytes) -> Any:
        return self.serde.loads_typed((type_tag, payload))

    def _expire_keys_sync(self, *keys: str) -> None:
        for key in keys:
            self._sync_client.expire(key, self._ttl_seconds)

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
                checkpoint_id = (
                    members[0].decode("utf-8") if isinstance(members[0], bytes) else str(members[0])
                )

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
            checkpoint_blob if isinstance(checkpoint_blob, bytes) else bytes(checkpoint_blob)
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
                    w_payload_raw if isinstance(w_payload_raw, bytes) else bytes(w_payload_raw)
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
                if filter is not None and any(tup.metadata.get(k) != v for k, v in filter.items()):
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
        metadata_type, metadata_blob = self._dump_typed(get_checkpoint_metadata(config, metadata))

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


__all__ = ["RedisCheckpointSaver"]
