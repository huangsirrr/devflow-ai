"""CI 分析上下文的两级缓存。

Redis 是共享层（docker-compose 里本来就有 redis 服务），进程内 LRU 是兜底层：
Redis 连不上就降级到内存，并把 devflow_ci_context_cache_up 置 0，不让一次
缓存故障变成一次 500。

缓存键里带日志内容的哈希，同步到新日志后键自然变化，不需要按前缀扫库删除。
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
from typing import Any
from urllib.parse import urlparse

from app.core.metrics import metrics

CACHE_UP = "devflow_ci_context_cache_up"
CACHE_REQUESTS = "devflow_ci_context_cache_requests_total"
CACHE_FALLBACK = "devflow_ci_context_cache_fallback_total"

metrics.describe(CACHE_UP, "1 表示配置的缓存后端可用，0 表示已降级到进程内缓存", "gauge")
metrics.describe(CACHE_REQUESTS, "CI 分析上下文缓存请求数", "counter")
metrics.describe(CACHE_FALLBACK, "Redis 不可用而降级到内存缓存的次数", "counter")


class CacheUnavailable(RuntimeError):
    """Redis 不可用，调用方应降级。"""


def cache_key(repo_id: Any, run_id: Any, conversation_id: Any, logs_text: str | None) -> str:
    digest = hashlib.sha256((logs_text or "").encode("utf-8")).hexdigest()[:16]
    conversation = str(conversation_id) if conversation_id else "-"
    return f"devflow:ci:ctx:{repo_id}:{run_id}:{conversation}:{digest}"


class MemoryCache:
    """带 TTL 和容量上限的进程内缓存，值进出一律深拷贝。"""

    def __init__(self, max_entries: int = 256) -> None:
        self.max_entries = max(1, int(max_entries))
        self._entries: dict[str, tuple[float | None, Any]] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Any | None:
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if expires_at is not None and expires_at <= time.monotonic():
                self._entries.pop(key, None)
                return None
            return copy.deepcopy(value)

    async def set(self, key: str, value: Any, ttl_seconds: float | None = None) -> None:
        async with self._lock:
            if key not in self._entries and len(self._entries) >= self.max_entries:
                victim = min(self._entries, key=lambda item: self._entries[item][0] or float("inf"))
                self._entries.pop(victim, None)
            expires_at = None if ttl_seconds is None else time.monotonic() + float(ttl_seconds)
            self._entries[key] = (expires_at, copy.deepcopy(value))

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._entries.pop(key, None)

    def size(self) -> int:
        return len(self._entries)


def _encode_command(*parts: str | bytes) -> bytes:
    encoded = [part.encode("utf-8") if isinstance(part, str) else part for part in parts]
    payload = bytearray(f"*{len(encoded)}\r\n".encode("ascii"))
    for part in encoded:
        payload += f"${len(part)}\r\n".encode("ascii") + part + b"\r\n"
    return bytes(payload)


async def _read_reply(reader: asyncio.StreamReader) -> Any:
    line = await reader.readuntil(b"\r\n")
    marker, body = line[:1], line[1:-2]
    if marker == b"+":
        return body.decode("utf-8")
    if marker == b"-":
        raise CacheUnavailable(body.decode("utf-8", "replace"))
    if marker == b":":
        return int(body)
    if marker == b"$":
        length = int(body)
        if length == -1:
            return None
        data = await reader.readexactly(length + 2)
        return data[:-2].decode("utf-8")
    if marker == b"*":
        count = int(body)
        if count == -1:
            return None
        return [await _read_reply(reader) for _ in range(count)]
    raise CacheUnavailable(f"unexpected RESP reply: {line!r}")


class RedisCache:
    """最小 RESP 客户端，只实现 GET / SET / PING。

    连接复用：第一次操作建连，之后复用同一条连接（操作间用锁串行化）。
    第一版是每次新建连接，压测发现 1.4 ms 里 90% 花在握手上。
    """

    def __init__(self, url: str, timeout_seconds: float = 1.5) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "redis":
            raise ValueError("redis_url must start with redis://")
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or 6379
        self.password = parsed.password
        self.db = int((parsed.path or "").strip("/") or 0)
        self.timeout = float(timeout_seconds)
        self._lock = asyncio.Lock()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def close(self) -> None:
        writer, self._writer, self._reader = self._writer, None, None
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001 - 关闭失败不影响结果
                pass

    async def _connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), self.timeout
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise CacheUnavailable(f"connect failed: {exc}") from exc
        try:
            if self.password:
                writer.write(_encode_command("AUTH", self.password))
                await writer.drain()
                await asyncio.wait_for(_read_reply(reader), self.timeout)
            if self.db:
                writer.write(_encode_command("SELECT", str(self.db)))
                await writer.drain()
                await asyncio.wait_for(_read_reply(reader), self.timeout)
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as exc:
            writer.close()
            raise CacheUnavailable(str(exc)) from exc
        self._reader, self._writer = reader, writer
        return reader, writer

    async def _execute(self, *parts: str) -> Any:
        async with self._lock:
            if self._writer is None or self._writer.is_closing():
                await self._connect()
            assert self._writer is not None and self._reader is not None
            try:
                self._writer.write(_encode_command(*parts))
                await self._writer.drain()
                return await asyncio.wait_for(_read_reply(self._reader), self.timeout)
            except CacheUnavailable:
                raise
            except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as exc:
                await self.close()
                raise CacheUnavailable(str(exc)) from exc

    async def ping(self) -> bool:
        return await self._execute("PING") == "PONG"

    async def get(self, key: str) -> Any | None:
        raw = await self._execute("GET", key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return None

    async def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        payload = json.dumps(value, ensure_ascii=False)
        await self._execute("SET", key, payload, "PX", str(max(1, int(ttl_seconds * 1000))))


class CiContextCache:
    """对外只暴露 get / set：优先 Redis，失败则降级到内存并打点。"""

    def __init__(
        self,
        *,
        enabled: bool = True,
        redis_url: str | None = None,
        ttl_seconds: float = 600.0,
        timeout_seconds: float = 1.5,
        max_entries: int = 256,
        retry_seconds: float = 30.0,
        memory: MemoryCache | None = None,
        redis: RedisCache | None = None,
    ) -> None:
        self.enabled = enabled
        self.ttl_seconds = float(ttl_seconds)
        self.retry_seconds = float(retry_seconds)
        self._memory = memory or MemoryCache(max_entries=max_entries)
        if redis is not None:
            self._redis: RedisCache | None = redis
        elif redis_url:
            self._redis = RedisCache(redis_url, timeout_seconds)
        else:
            self._redis = None
        self._redis_retry_at = 0.0

    @property
    def backend(self) -> str:
        if self._redis is None or time.monotonic() < self._redis_retry_at:
            return "memory"
        return "redis"

    async def get(self, key: str) -> Any | None:
        if not self.enabled:
            return None
        if self.backend == "redis":
            try:
                value = await self._redis.get(key)  # type: ignore[union-attr]
            except CacheUnavailable:
                self._degrade()
            else:
                metrics.set(CACHE_UP, 1)
                metrics.inc(CACHE_REQUESTS, result="hit" if value is not None else "miss", backend="redis")
                return value
        value = await self._memory.get(key)
        metrics.inc(CACHE_REQUESTS, result="hit" if value is not None else "miss", backend="memory")
        return value

    async def set(self, key: str, value: Any) -> None:
        if not self.enabled:
            return
        if self.backend == "redis":
            try:
                await self._redis.set(key, value, self.ttl_seconds)  # type: ignore[union-attr]
            except CacheUnavailable:
                self._degrade()
            else:
                metrics.set(CACHE_UP, 1)
                return
        await self._memory.set(key, value, self.ttl_seconds)

    def _degrade(self) -> None:
        self._redis_retry_at = time.monotonic() + self.retry_seconds
        metrics.set(CACHE_UP, 0)
        metrics.inc(CACHE_FALLBACK)
