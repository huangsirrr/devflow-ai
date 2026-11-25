"""对比 CI 分析上下文缓存的两条路径：命中 vs 未命中。

用法（在 backend 目录）：
    python scripts/bench_ci_cache.py            # 只测内存档
    python scripts/bench_ci_cache.py --redis    # 额外测 Redis（本机 6379 没起就跳过）

输出三件事：各路径的耗时中位数、命中/未命中的计数变化、降级状态。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.metrics import metrics
from app.services.ci_context_cache import CiContextCache, cache_key


def _sample_context(entries: int = 60) -> dict:
    """造一份体积接近真实 CI 上下文的负载：代码片段 + 文档片段 + 计数。"""
    evidence = [
        {
            "source_type": "code",
            "title": f"src/module_{index}.py",
            "snippet": ("def handler(request):\n    return service.call(request)\n" * 4),
            "score": 0.83,
        }
        for index in range(entries)
    ]
    return {
        "query": "TypeError: 'NoneType' object is not subscriptable in handler",
        "context": {"code_references": evidence, "failed_jobs": [{"name": "unit-test", "conclusion": "failure"}]},
        "counts": {"failed_jobs": 1, "failed_steps": 2, "code": len(evidence)},
    }


async def _time(callable_, rounds: int) -> list[float]:
    samples: list[float] = []
    for _ in range(rounds):
        started = time.perf_counter()
        await callable_()
        samples.append((time.perf_counter() - started) * 1000)
    return samples


async def main(use_redis: bool) -> int:
    payload = _sample_context()
    size_kb = len(json.dumps(payload, ensure_ascii=False)) / 1024
    key = cache_key(uuid.uuid4(), uuid.uuid4(), None, "TypeError: 'NoneType' object is not subscriptable")
    rounds = 50

    results: dict[str, dict] = {}

    memory_cache = CiContextCache(redis_url=None, ttl_seconds=600)
    await memory_cache.set(key, payload)
    await memory_cache.set(key, payload)
    samples = await _time(lambda: memory_cache.get(key), rounds)
    results["memory"] = {"median_ms": statistics.median(samples), "p95_ms": sorted(samples)[int(rounds * 0.95) - 1]}

    if use_redis:
        redis_cache = CiContextCache(redis_url="redis://127.0.0.1:6379/0", ttl_seconds=600)
        try:
            await redis_cache.set(key, payload)
        except Exception as exc:  # noqa: BLE001 - 演示脚本要能容忍 Redis 没起
            print(f"[skip] Redis 不可用：{exc}")
        else:
            if redis_cache.backend == "redis":
                await redis_cache.set(key, payload)
                samples = await _time(lambda: redis_cache.get(key), rounds)
                results["redis"] = {
                    "median_ms": statistics.median(samples),
                    "p95_ms": sorted(samples)[int(rounds * 0.95) - 1],
                }
            else:
                print("[skip] Redis 连接失败，已降级到内存")

    print(f"\n负载大小：{size_kb:.1f} KB / 每条；每档 {rounds} 次读取")
    print("-" * 52)
    for backend, stats in results.items():
        print(f"{backend:8s} 中位数 {stats['median_ms']:.3f} ms   p95 {stats['p95_ms']:.3f} ms")
    print("-" * 52)
    print(metrics.render().strip())
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--redis", action="store_true", help="额外测本机 Redis")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.redis)))
