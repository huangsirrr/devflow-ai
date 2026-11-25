import asyncio

import pytest

from app.core.metrics import metrics
from app.services import ci_context_cache as cache_module
from app.services.ci_context_cache import CACHE_UP, CiContextCache, MemoryCache, cache_key


def test_cache_key_changes_with_logs() -> None:
    first = cache_key("repo-1", "run-1", None, "error: boom")
    second = cache_key("repo-1", "run-1", None, "error: other")

    assert first != second
    assert first.startswith("devflow:ci:ctx:repo-1:run-1:-:")


def test_cache_key_includes_conversation() -> None:
    assert cache_key("r", "u", None, "x") != cache_key("r", "u", "conv-1", "x")


@pytest.mark.asyncio
async def test_memory_cache_returns_deep_copies() -> None:
    cache = MemoryCache(max_entries=4)
    await cache.set("k", {"items": []})

    first = await cache.get("k")
    first["items"].append(1)

    assert await cache.get("k") == {"items": []}


@pytest.mark.asyncio
async def test_memory_cache_expires() -> None:
    cache = MemoryCache()
    await cache.set("k", {"v": 1}, ttl_seconds=0.01)

    assert await cache.get("k") == {"v": 1}
    await asyncio.sleep(0.05)
    assert await cache.get("k") is None


@pytest.mark.asyncio
async def test_memory_cache_evicts_when_full() -> None:
    cache = MemoryCache(max_entries=2)
    await cache.set("a", 1)
    await cache.set("b", 2)
    await cache.set("c", 3)

    assert cache.size() == 2
    assert await cache.get("c") == 3


@pytest.mark.asyncio
async def test_unreachable_redis_falls_back_to_memory() -> None:
    metrics.reset()
    cache = CiContextCache(redis_url="redis://127.0.0.1:1/0", timeout_seconds=0.2, ttl_seconds=5)

    await cache.set("k", {"v": 1})

    assert await cache.get("k") == {"v": 1}
    assert cache.backend == "memory"
    assert metrics.get(CACHE_UP) == 0
    assert metrics.get("devflow_ci_context_cache_requests_total", result="hit", backend="memory") == 1


@pytest.mark.asyncio
async def test_cache_counts_hit_and_miss() -> None:
    metrics.reset()
    cache = CiContextCache(redis_url=None, ttl_seconds=5)

    await cache.set("k", {"v": 1})
    assert await cache.get("k") == {"v": 1}
    assert await cache.get("missing") is None

    text = metrics.render()
    assert 'backend="memory",result="hit"} 1' in text
    assert 'backend="memory",result="miss"} 1' in text


def test_resp_bulk_string_reply() -> None:
    async def scenario() -> str | None:
        reader = asyncio.StreamReader()
        reader.feed_data(b"$5\r\nhello\r\n")
        reader.feed_eof()
        return await cache_module._read_reply(reader)

    assert asyncio.run(scenario()) == "hello"


def test_resp_error_reply_raises() -> None:
    async def scenario() -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(b"-ERR wrong type\r\n")
        reader.feed_eof()
        await cache_module._read_reply(reader)

    with pytest.raises(cache_module.CacheUnavailable):
        asyncio.run(scenario())


def test_resp_null_and_array_replies() -> None:
    async def null_reply() -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(b"$-1\r\n")
        reader.feed_eof()
        return await cache_module._read_reply(reader)

    async def array_reply() -> list:
        reader = asyncio.StreamReader()
        reader.feed_data(b"*2\r\n$3\r\nfoo\r\n:7\r\n")
        reader.feed_eof()
        return await cache_module._read_reply(reader)

    assert asyncio.run(null_reply()) is None
    assert asyncio.run(array_reply()) == ["foo", 7]
