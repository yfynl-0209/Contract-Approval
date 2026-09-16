"""作业唤醒设施的组合根（M9 Task 5）。

进程内**一个** `JobNotifier` 实例：
- 入口进程（API / Worker）启动时 `set_job_notifier(build_job_notifier_from_settings())`；
- 业务代码（`create_job`）与 Worker 循环只通过 `get_job_notifier()` 取用 ——
  它们不知道背后是 Redis 还是纯轮询。

未配置 `REDIS_URL` 时装配 `NullJobNotifier`：行为与 M9 之前完全一致
（纯 DB 轮询）。
"""

from __future__ import annotations

from app.config import settings
from app.ports.job_notifier import NULL_JOB_NOTIFIER, JobNotifier, NullJobNotifier

#: 进程级单例。None = 尚未装配（get 时兜底为 NULL —— 测试环境无入口进程也安全）
_job_notifier: JobNotifier | None = None


def set_job_notifier(notifier: JobNotifier) -> None:
    """入口进程启动时装配。⚠️ 重复装配会被拒绝 —— 装配只该发生一次。"""
    global _job_notifier
    if _job_notifier is not None and _job_notifier is not notifier:
        raise RuntimeError("JobNotifier 已装配：组合根只允许装配一次")
    _job_notifier = notifier


def get_job_notifier() -> JobNotifier:
    """业务代码取用点。未装配时兜底 NULL —— 绝不因"忘了装配"而失败。"""
    return _job_notifier or NULL_JOB_NOTIFIER


def build_job_notifier_from_settings() -> JobNotifier:
    """按配置装配：`REDIS_URL` 未配置 → 纯轮询（M9 之前的语义）。"""
    if not settings.redis_url:
        return NullJobNotifier()
    from app.adapters.queue.redis_notifier import RedisJobNotifier

    return RedisJobNotifier(
        url=settings.redis_url,
        namespace=f"{settings.env}:{settings.tenant_id}",
    )


def reset_job_notifier() -> None:
    """仅供测试：清空进程级装配。"""
    global _job_notifier
    _job_notifier = None
