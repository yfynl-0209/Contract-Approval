"""`JobNotifier` 的 Redis 实现（M9 Task 5）。

## 机制选型

- **唤醒**：`LPUSH`（通知方）+ `BLPOP with timeout`（Worker）。
  选 LIST 而不是 pub/sub：pub/sub 的订阅生命周期管理复杂（每次轮询
  一对 subscribe/unsubscribe），且离线消息会**永久丢失**；LIST 的
  消息至少会被下一次 BLPOP 消费掉（多余的消息醒来空转一轮，无害）。
- **锁**：`SET NX EX`（原子获取，TTL 防死锁）+ 比较后删除的 Lua 脚本
  （只释放自己持有的锁 —— 否则慢持有者的超时锁会被新持有者释放）。
- **缓存**：`SETEX`，强制 TTL（计划要求：锁与缓存必须有过期）。

## 命名空间

所有键 / 通道都带 `{env}:{tenant}` 前缀 —— 共享一台 Redis 的两个环境
（dev / prod）不得互相唤醒或互抢锁（计划 Task 5 明确要求）。

## 故障纪律（见端口文档）

每个方法都把 Redis 故障**吞掉并降级**：wait → 立即返回（落回轮询）、
notify/cache/lock → 无操作或返回未命中。Redis 的死活不影响业务正确性。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import redis as redis_lib

from app.ports.job_notifier import NULL_JOB_NOTIFIER, JobNotifier

logger = logging.getLogger(__name__)


class RedisJobNotifier:
    """Redis 实现的唤醒 / 锁 / 有界缓存。

    任何 Redis 故障都降级为 `NullJobNotifier` 的等价行为 ——
    通过在方法内部捕获异常实现，而不是在调用方到处 try。
    """

    def __init__(self, *, url: str, namespace: str) -> None:
        """
        Args:
            url: 形如 `redis://127.0.0.1:56379/0`。
            namespace: `{env}:{tenant}` —— 所有键与通道的前缀。
        """
        self._client = redis_lib.Redis.from_url(
            url, decode_responses=True, socket_connect_timeout=2.0
        )
        self._namespace = namespace.strip().strip(":")

    # ------------------------------------------------------------------
    # 键名
    # ------------------------------------------------------------------
    def _wakeup_key(self, job_type: str) -> str:
        return f"{self._namespace}:wakeup:{job_type}"

    def _cache_key(self, key: str) -> str:
        return f"{self._namespace}:cache:{key}"

    def _lock_key(self, name: str) -> str:
        return f"{self._namespace}:lock:{name}"

    # ------------------------------------------------------------------
    # 唤醒
    # ------------------------------------------------------------------
    def notify_job_created(self, job_type: str) -> None:
        try:
            self._client.lpush(self._wakeup_key(job_type), "1")
            # 唤醒列表不需要积压历史：只要"有一个待消费信号"即可。
            # 修剪到 1 个元素，防止通知堆积让 Worker 连续空转多轮。
            self._client.ltrim(self._wakeup_key(job_type), 0, 0)
        except redis_lib.RedisError as exc:
            logger.warning("Redis 唤醒失败（退回 DB 轮询）：%s", exc)

    def wait_for_job(self, job_types: Sequence[str], timeout: float) -> None:
        if timeout <= 0:
            return
        keys = [self._wakeup_key(t) for t in job_types]
        try:
            result = self._client.blpop(keys, timeout=timeout)
        except redis_lib.RedisError as exc:
            # ⚠️ 与"通知丢失"同一条纪律：等不到就立即返回，
            # Worker 落回 DB 轮询 —— Redis 挂掉不得拖住 Worker
            logger.warning("Redis 等待失败（立即返回轮询）：%s", exc)
            return
        if result is None:
            return  # 超时：正常路径，Worker 直接进入下一轮轮询

    # ------------------------------------------------------------------
    # 有界缓存
    # ------------------------------------------------------------------
    def cache_get(self, key: str) -> str | None:
        try:
            return self._client.get(self._cache_key(key))
        except redis_lib.RedisError as exc:
            logger.warning("Redis 缓存读失败（按未命中处理）：%s", exc)
            return None

    def cache_set(self, key: str, value: str, *, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            raise ValueError(f"ttl_seconds 必须为正数，收到 {ttl_seconds}")
        try:
            self._client.setex(self._cache_key(key), ttl_seconds, value)
        except redis_lib.RedisError as exc:
            logger.warning("Redis 缓存写失败（数据仍在数据库）：%s", exc)

    # ------------------------------------------------------------------
    # 锁
    # ------------------------------------------------------------------
    #: 只删除自己持有的锁：值比对 + 删除必须原子（否则存在
    #: "持有者 A 超时 → B 拿到锁 → A 醒来删掉了 B 的锁"的窗口）
    _RELEASE_SCRIPT = """
    if redis.call('get', KEYS[1]) == ARGV[1] then
        return redis.call('del', KEYS[1])
    end
    return 0
    """

    def acquire_lock(self, name: str, *, ttl_seconds: float) -> bool:
        if ttl_seconds <= 0:
            raise ValueError(f"ttl_seconds 必须为正数，收到 {ttl_seconds}")
        holder = _holder_token()
        try:
            acquired = self._client.set(
                self._lock_key(name),
                holder,
                nx=True,
                ex=max(1, round(ttl_seconds)),
            )
        except redis_lib.RedisError as exc:
            logger.warning("Redis 取锁失败（按未取得处理）：%s", exc)
            return False
        if not acquired:
            return False
        # 记住自己的令牌，release 时只删自己这把
        _LOCAL_LOCK_HOLDERS[name] = holder
        return True

    def release_lock(self, name: str) -> None:
        holder = _LOCAL_LOCK_HOLDERS.pop(name, None)
        if holder is None:
            return
        try:
            self._client.eval(self._RELEASE_SCRIPT, 1, self._lock_key(name), holder)
        except redis_lib.RedisError as exc:
            logger.warning("Redis 放锁失败（等 TTL 过期）：%s", exc)


#: 本进程当前持有的锁令牌（name → token）。
#: 锁的互斥由 Redis 保证；这份表只服务"释放自己的锁"。
_LOCAL_LOCK_HOLDERS: dict[str, str] = {}


def _holder_token() -> str:
    import uuid

    return uuid.uuid4().hex


def build_job_notifier(*, redis_url: str | None, namespace: str) -> JobNotifier:
    """组合根工厂：`REDIS_URL` 未配置 → 纯轮询兜底。"""
    if not redis_url:
        return NULL_JOB_NOTIFIER
    return RedisJobNotifier(url=redis_url, namespace=namespace)
