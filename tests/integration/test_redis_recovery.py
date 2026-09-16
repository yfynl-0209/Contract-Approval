"""Redis 故障恢复（M9 Task 5）—— 三条"Redis 死了业务也不能死"的证明。

1. **通知唤醒，但领取仍以数据库为准**：唤醒后 Worker 照常领取并校验 DB 行；
2. **Redis 断连时轮询兜底**：作业提交后 Redis 不可达，作业仍被处理；
3. **缓存清空不丢数据**：删光 Redis 缓存，结果/历史仍然可读。

Redis 用真容器（m9-redis，端口 56379）；不可达时整组跳过。
"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from pathlib import PurePosixPath

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.adapters.queue.redis_notifier import RedisJobNotifier
from app.config import PROJECT_ROOT
from app.enums import JobStatus, JobType
from app.models import ApprovalTask, WorkflowJob
from app.worker import Worker

REDIS_URL = "redis://127.0.0.1:56379/0"
SCHEMA_FILE = PROJECT_ROOT / "db" / "schema.sql"


@pytest.fixture()
def session_factory(work_dir: Path) -> sessionmaker:
    """独立临时库（schema.sql 建表）—— 与 parse-worker 集成测试同一套路。"""
    path = work_dir / "redis-recovery.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()
    engine = create_engine(f"sqlite:///{PurePosixPath(path.as_posix())}", future=True)
    try:
        yield sessionmaker(bind=engine, future=True)
    finally:
        engine.dispose()


def _redis_available() -> bool:
    try:
        notifier = RedisJobNotifier(url=REDIS_URL, namespace="probe")
        notifier.cache_set("probe", "1", ttl_seconds=5)
        return notifier.cache_get("probe") == "1"
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(
    not _redis_available(), reason="Redis 不可达（M9 开发容器未启动？端口 56379）"
)


def _seed_queued_job(session_factory, *, instance_id: str) -> int:
    session = session_factory()
    try:
        task = ApprovalTask(
            provider="mock",
            tenant_id="default",
            instance_id=instance_id,
            approval_code=instance_id,
        )
        session.add(task)
        session.flush()
        job = WorkflowJob(
            task_id=task.id,
            job_type=JobType.PARSE.value,
            job_status=JobStatus.QUEUED.value,
            idempotency_key=f"redis-recovery:{instance_id}",
            input_json="{}",
            input_digest="d",
            max_attempts=3,
        )
        session.add(job)
        session.commit()
        return job.id
    finally:
        session.close()


def test_notification_wakes_worker_which_still_validates_the_db_row(session_factory) -> None:
    """唤醒后 Worker 仍以 DB 为准：没有可领作业时，空转而不是编造工作。"""
    notifier = RedisJobNotifier(url=REDIS_URL, namespace="test:wake")
    worker = Worker(session_factory, handler=lambda run: None, notifier=notifier)

    notifier.notify_job_created("parse")  # 有信号……
    assert worker.run_once() is False  # ……但库里没有作业 → 不做任何事

    _seed_queued_job(session_factory, instance_id="HT-REDIS-1")
    notifier.notify_job_created("parse")
    assert worker.run_once() is True  # 信号 + DB 行都在 → 正常领取


def test_worker_still_processes_jobs_when_redis_is_down(session_factory) -> None:
    """Redis 不可达：notify 静默吞掉、wait 立即返回，作业仍被轮询处理。"""
    # 指向一个必然无人监听的端口
    dead_notifier = RedisJobNotifier(url="redis://127.0.0.1:1/0", namespace="test:down")
    job_id = _seed_queued_job(session_factory, instance_id="HT-REDIS-2")

    dead_notifier.notify_job_created("parse")  # 不应抛出

    worker = Worker(
        session_factory,
        handler=lambda run: None,
        notifier=dead_notifier,
        poll_interval=0.05,
    )
    assert worker.run_once() is True, "Redis 挂掉不得影响作业领取"

    session = session_factory()
    try:
        job = session.get(WorkflowJob, job_id)
        assert job.job_status == JobStatus.SUCCEEDED.value
    finally:
        session.close()


def test_clearing_cache_keeps_history_available(session_factory) -> None:
    """删光缓存：数据库里的结果与历史不受影响（Redis 不是任何数据的家）。"""
    notifier = RedisJobNotifier(url=REDIS_URL, namespace="test:cache")
    job_id = _seed_queued_job(session_factory, instance_id="HT-REDIS-3")

    # Worker 把"最后处理的作业 id"写进缓存（模拟有界缓存的使用方式）
    notifier.cache_set("last_job", str(job_id), ttl_seconds=60)
    assert notifier.cache_get("last_job") == str(job_id)

    # "删库"：换一个命名空间（等效于缓存被清空/Redis 重启丢数据）
    fresh = RedisJobNotifier(url=REDIS_URL, namespace="test:cache-after-flush")
    assert fresh.cache_get("last_job") is None

    # 业务数据安然无恙，且处理历史可从 DB 复核
    session = session_factory()
    try:
        job = session.get(WorkflowJob, job_id)
        assert job is not None
        assert job.job_status == JobStatus.QUEUED.value  # 从未被缓存承载
        session.delete(job)  # 证明它完全由 DB 所有：删 DB 行即删事实
        session.commit()
        assert session.get(WorkflowJob, job_id) is None
    finally:
        session.close()


def test_concurrent_notify_and_wait_do_not_deadlock(session_factory) -> None:
    """通知方与等待方并发：wait 在超时内返回，进程不卡死。"""

    def waiter():
        notifier = RedisJobNotifier(url=REDIS_URL, namespace="test:race")
        started = _now()
        notifier.wait_for_job(["parse"], timeout=1.0)
        return _now() - started

    def talker():
        notifier = RedisJobNotifier(url=REDIS_URL, namespace="test:race")
        notifier.notify_job_created("parse")

    with ThreadPoolExecutor(max_workers=2) as pool:
        wait_future = pool.submit(waiter)
        pool.submit(talker)
        elapsed = wait_future.result(timeout=5)

    assert elapsed <= 2.0, "wait 必须在通知或超时二者取先地返回"


def _now() -> float:
    import time

    return time.monotonic()
