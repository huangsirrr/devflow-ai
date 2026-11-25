"""用进程内 RESP 服务器验证 Redis 分支，不依赖本机装 Redis。"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from app.core.metrics import metrics
from app.services.ci_context_cache import CACHE_UP, CiContextCache


class FakeRedisServer:
    """只实现 PING / GET / SET(PX) / SELECT / AUTH 的最小 Redis。"""

    def __init__(self) -> None:
        self._store: dict[str, tuple[str, float | None]] = {}
        self._server: asyncio.AbstractServer | None = None
        self._connections: set[asyncio.StreamWriter] = set()
        self.port = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        # 先断开已有连接，否则 wait_closed() 会一直等 handler 结束。
        for writer in list(self._connections):
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        self._connections.clear()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception, asyncio.TimeoutError):
                await asyncio.wait_for(self._server.wait_closed(), 2)

    @property
    def url(self) -> str:
        return f"redis://127.0.0.1:{self.port}/0"

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._connections.add(writer)
        try:
            while True:
                command = await self._read_command(reader)
                if command is None:
                    return
                reply = self._execute(command)
                writer.write(reply)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            return
        finally:
            self._connections.discard(writer)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _read_command(self, reader: asyncio.StreamReader) -> list[str] | None:
        header = await reader.readline()
        if not header:
            return None
        if not header.startswith(b"*"):
            raise AssertionError(f"unexpected command header: {header!r}")
        count = int(header[1:-2])
        parts: list[str] = []
        for _ in range(count):
            length = int((await reader.readline())[1:-2])
            payload = await reader.readexactly(length)
            await reader.readexactly(2)
            parts.append(payload.decode("utf-8"))
        return parts

    def _execute(self, command: list[str]) -> bytes:
        name = command[0].upper()
        if name == "PING":
            return b"+PONG\r\n"
        if name in ("SELECT", "AUTH"):
            return b"+OK\r\n"
        if name == "SET":
            key, value = command[1], command[2]
            ttl_ms: float | None = None
            if len(command) >= 5 and command[3].upper() == "PX":
                ttl_ms = float(command[4])
            expires_at = None if ttl_ms is None else asyncio.get_running_loop().time() + ttl_ms / 1000
            self._store[key] = (value, expires_at)
            return b"+OK\r\n"
        if name == "GET":
            entry = self._store.get(command[1])
            if entry is None:
                return b"$-1\r\n"
            value, expires_at = entry
            if expires_at is not None and asyncio.get_running_loop().time() >= expires_at:
                self._store.pop(command[1], None)
                return b"$-1\r\n"
            payload = value.encode("utf-8")
            return f"${len(payload)}\r\n".encode() + payload + b"\r\n"
        return b"-ERR unsupported\r\n"


@pytest.mark.asyncio
async def test_cache_reads_and_writes_through_redis() -> None:
    metrics.reset()
    server = FakeRedisServer()
    await server.start()
    try:
        cache = CiContextCache(redis_url=server.url, ttl_seconds=5, timeout_seconds=2)
        assert cache.backend == "redis"

        await cache.set("ctx:1", {"query": "boom", "counts": {"code": 3}})
        assert await cache.get("ctx:1") == {"query": "boom", "counts": {"code": 3}}
        assert await cache.get("ctx:missing") is None

        assert metrics.get(CACHE_UP) == 1
        assert metrics.get("devflow_ci_context_cache_requests_total", result="hit", backend="redis") == 1
        assert metrics.get("devflow_ci_context_cache_requests_total", result="miss", backend="redis") == 1
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_redis_entry_expires_after_ttl() -> None:
    server = FakeRedisServer()
    await server.start()
    try:
        cache = CiContextCache(redis_url=server.url, ttl_seconds=0.05, timeout_seconds=2)
        await cache.set("ctx:ttl", {"v": 1})

        assert await cache.get("ctx:ttl") == {"v": 1}
        await asyncio.sleep(0.12)
        assert await cache.get("ctx:ttl") is None
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_redis_outage_degrades_then_recovers() -> None:
    metrics.reset()
    server = FakeRedisServer()
    await server.start()
    cache = CiContextCache(redis_url=server.url, ttl_seconds=5, timeout_seconds=0.5, retry_seconds=0.05)
    try:
        await cache.set("ctx:a", {"v": 1})
        await server.stop()

        assert await cache.get("ctx:a") is None  # 连接失败 -> 降级到内存（此时内存里没有）
        assert cache.backend == "memory"
        assert metrics.get(CACHE_UP) == 0
        assert metrics.get("devflow_ci_context_cache_fallback_total") == 1

        await cache.set("ctx:b", {"v": 2})
        assert await cache.get("ctx:b") == {"v": 2}
    finally:
        await server.stop()
