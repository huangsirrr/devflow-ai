"""真实 Redis 的集成测试：本机没起 Redis 时自动跳过。

docker compose up -d redis 之后再跑这个文件，验证协议实现与真实服务兼容。
"""

from __future__ import annotations

import asyncio

import pytest

from app.services.ci_context_cache import CacheUnavailable, CiContextCache, RedisCache


async def _redis_available(url: str) -> bool:
    client = RedisCache(url, timeout_seconds=0.5)
    try:
        return await client.ping()
    except CacheUnavailable:
        return False
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_roundtrip_against_real_redis() -> None:
    url = "redis://127.0.0.1:6379/0"
    if not await _redis_available(url):
        pytest.skip("本机没有可用的 Redis，跳过集成测试")

    cache = CiContextCache(redis_url=url, ttl_seconds=30, timeout_seconds=1)
    key = "devflow:test:ci_context:roundtrip"
    payload = {"query": "boom", "context": {"code": [1, 2, 3]}, "counts": {"code": 3}}
    try:
        await cache.set(key, payload)
        assert await cache.get(key) == payload
        assert cache.backend == "redis"
    finally:
        client = RedisCache(url, timeout_seconds=1)
        try:
            await client._execute("DEL", key)  # noqa: SLF001 - 测试清理
        finally:
            await client.close()
