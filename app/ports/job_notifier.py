"""作业唤醒与加速设施端口（M9 Task 5）。

## ⚠️ 本端口的每一条方法都**允许失败**、**允许丢**

`workflow_jobs` 表永远是作业的唯一真相来源（M3 设计确认 §"M9" 行的原文：
"Redis 只加速、不改变语义"）。因此：

- **通知丢失** → Worker 退回轮询，作业最多晚一个 `poll_interval` 被处理；
- **缓存丢失** → 命中率下降，数据仍在数据库里；
- **锁丢失** → 数据库的唯一约束与租约条件仍是真正的并发防线。

**任何实现不得让上述故障反向伤害主流程**：Redis 断连时，`wait` 必须
立即返回（让 Worker 落回轮询），`notify` 必须静默吞掉，绝不能让
"通知失败"把已经提交的作业变成故障。

不配置 Redis 时用 `NullJobNotifier`：行为退化为纯 DB 轮询 ——
与 M9 之前的语义完全一致。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable


@runtime_checkable
class JobNotifier(Protocol):
    """作业唤醒 / 锁 / 有界缓存的端口。"""

    def notify_job_created(self, job_type: str) -> None:
        """一个新作业已创建（可能尚未提交）。实现必须**吞掉自身的一切故障**。"""
        ...

    def wait_for_job(self, job_types: Sequence[str], timeout: float) -> None:
        """阻塞直到某个类型可能有新作业、或超时 —— **二者取先，必须返回**。

        ⚠️ 返回不代表真有作业：Worker 醒来后仍要**照常领取并校验
        数据库行**（状态 / 租约 / 幂等）。被虚假唤醒是正常路径。
        """
        ...

    def cache_get(self, key: str) -> str | None:
        """读有界缓存；任何故障返回 None（调用方按"未命中"走数据库）。"""
        ...

    def cache_set(self, key: str, value: str, *, ttl_seconds: int) -> None:
        """写有界缓存（必须带 TTL）。实现必须吞掉自身故障。"""
        ...

    def acquire_lock(self, name: str, *, ttl_seconds: float) -> bool:
        """尝试获取带 TTL 的锁（防死锁：持有者崩溃后锁自动过期）。

        返回 False = 已被他人持有。**锁只是优化**（如避免重复扫描），
        正确性不得依赖它。
        """
        ...

    def release_lock(self, name: str) -> None:
        """释放锁；只应释放自己持有的（实现须保证），故障静默吞掉。"""
        ...


class NullJobNotifier:
    """无 Redis 时的兜底实现：一切退化为纯 DB 轮询。"""

    def notify_job_created(self, job_type: str) -> None:
        return None

    def wait_for_job(self, job_types: Sequence[str], timeout: float) -> None:
        # 直接返回：调用方（Worker）随即进入下一轮 DB 轮询 ——
        # 与 M9 之前的 `stop_event.wait(poll_interval)` 行为等价
        return None

    def cache_get(self, key: str) -> str | None:
        return None

    def cache_set(self, key: str, value: str, *, ttl_seconds: int) -> None:
        # 与 Redis 实现同一契约：ttl 非正是调用方编程错误，两个实现都抛
        if ttl_seconds <= 0:
            raise ValueError(f"ttl_seconds 必须为正数，收到 {ttl_seconds}")
        return None

    def acquire_lock(self, name: str, *, ttl_seconds: float) -> bool:
        # 无共享存储就没有真互斥：返回"拿到"让调用方继续，
        # 正确性由数据库约束兜底（本类只该用在单实例部署）
        return True

    def release_lock(self, name: str) -> None:
        return None


NULL_JOB_NOTIFIER = NullJobNotifier()
