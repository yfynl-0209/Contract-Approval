"""`JobNotifier` 合约（M9 Task 5）。

与存储合约同一思路：`NullJobNotifier` 与 `RedisJobNotifier` 必须满足
同一套**可替换语义**。差异点只有一个且是**允许的**：Null 没有真唤醒 ——
它的 `wait_for_job` 直接返回（调用方随即落回 DB 轮询），
因此唤醒类断言只对 Redis 实现跑（见 `tests/integration/`）。
"""

from __future__ import annotations

import time

import pytest

from app.ports.job_notifier import NullJobNotifier


class NullJobNotifierContract:
    """`NullJobNotifier`（纯轮询兜底）的行为。"""

    @pytest.fixture()
    def notifier(self) -> NullJobNotifier:
        return NullJobNotifier()

    def test_notify_is_silent_no_op(self, notifier: NullJobNotifier) -> None:
        """通知不报错、不产生任何可见效果 —— 通知丢失必须是常态路径。"""
        notifier.notify_job_created("parse")  # 不应抛出

    def test_wait_returns_promptly(self, notifier: NullJobNotifier) -> None:
        """`wait` 必须立即返回（Worker 随即进入下一轮 DB 轮询）——
        兜底实现绝不能阻塞。"""
        started = time.monotonic()
        notifier.wait_for_job(["parse"], timeout=0.05)
        assert time.monotonic() - started < 0.5

    def test_cache_miss_then_set_is_still_miss(self, notifier: NullJobNotifier) -> None:
        """兜底实现没有存储：set 之后 get 仍是未命中 —— 缓存丢失不影响正确性。"""
        assert notifier.cache_get("k") is None
        notifier.cache_set("k", "v", ttl_seconds=60)
        assert notifier.cache_get("k") is None

    def test_cache_set_requires_positive_ttl(self, notifier: NullJobNotifier) -> None:
        with pytest.raises(ValueError):
            notifier.cache_set("k", "v", ttl_seconds=0)

    def test_lock_always_acquired_and_release_is_silent(
        self, notifier: NullJobNotifier
    ) -> None:
        """单实例兜底：锁总是"拿到"，释放是静默无操作。

        ⚠️ 这只对**单进程部署**成立 —— 正确性由数据库约束兜底，
        多实例部署必须配置 Redis。
        """
        assert notifier.acquire_lock("scan", ttl_seconds=5) is True
        notifier.release_lock("scan")  # 不应抛出


class TestNullJobNotifier(NullJobNotifierContract):
    pass
