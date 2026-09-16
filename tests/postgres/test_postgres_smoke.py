"""PostgreSQL 冒烟测试（M9 Task 1）。

覆盖计划要求的五项：连接、事务回滚、外键、时间戳、并发作业领取。
生产代码零改动即可通过 —— 这正是"适配器式迁移"的验收方式：
**同样的业务函数，在两种方言下行为一致**。

并发领取用的是**生产领取函数** `claim_next_job`（不是测试专用路径）：
它当前是 SQLite 语义（子查询候选 + 条件 UPDATE），本文件先证明它在
PG 单并发下同样正确；两真并发下的重复领取问题归 Task 3 修 + 钉。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.enums import JobStatus, JobType
from app.models import ApprovalAttachment, ApprovalTask, WorkflowJob
from app.worker import claim_next_job


def _make_task(session: Session) -> ApprovalTask:
    task = ApprovalTask(
        provider="mock",
        tenant_id="default",
        instance_id="HT-PG-1",
        approval_code="HT-PG-0001",
    )
    session.add(task)
    session.flush()
    return task


# ------------------------------------------------------------
# 连接与基本读写
# ------------------------------------------------------------


def test_pg_connection_and_basic_write(pg_session: Session) -> None:
    task = _make_task(pg_session)
    pg_session.commit()

    loaded = pg_session.get(ApprovalTask, task.id)
    assert loaded is not None
    assert loaded.approval_code == "HT-PG-0001"
    # PostgreSQL 方言下 IDENTITY/序列正常发号
    assert task.id > 0


# ------------------------------------------------------------
# 事务回滚
# ------------------------------------------------------------


def test_pg_transaction_rollback_discards_uncommitted_writes(
    pg_session: Session,
) -> None:
    task = _make_task(pg_session)
    pg_session.commit()
    before = pg_session.execute(select(func.count()).select_from(ApprovalTask)).scalar_one()

    pg_session.add(
        ApprovalTask(
            provider="mock",
            tenant_id="default",
            instance_id="HT-PG-ROLLBACK",
            approval_code="HT-PG-ROLLBACK",
        )
    )
    pg_session.flush()
    mid = pg_session.execute(select(func.count()).select_from(ApprovalTask)).scalar_one()
    assert mid == before + 1  # flush 后本连接可见

    pg_session.rollback()
    after = pg_session.execute(select(func.count()).select_from(ApprovalTask)).scalar_one()
    assert after == before  # 回滚后消失


# ------------------------------------------------------------
# 外键约束（SQLite 需 PRAGMA，PG 天生强制 —— 这里证明复合外键在 PG 下生效）
# ------------------------------------------------------------


def test_pg_foreign_keys_enforced(pg_session: Session) -> None:
    task = _make_task(pg_session)
    # attachment_id 引用不存在的任务 → 复合外键必须拒绝
    pg_session.add(
        ApprovalAttachment(
            task_id=99999,  # 不存在
            attachment_id="ATT-X",
            file_name="a.pdf",
            object_key="k",
            file_checksum="0" * 64,
            download_status="success",
            content_type="application/pdf",
        )
    )
    with pytest.raises(IntegrityError):
        pg_session.commit()
    pg_session.rollback()


# ------------------------------------------------------------
# 时间戳（server_default / 时区列在 PG 下的真实行为）
# ------------------------------------------------------------


def test_pg_server_timestamps(pg_session: Session) -> None:
    task = _make_task(pg_session)
    pg_session.commit()
    assert task.created_at is not None, "server_default 必须在 PG 下生效"
    assert task.updated_at is not None


# ------------------------------------------------------------
# 并发作业领取（生产领取函数在 PG 下的单并发正确性）
# ------------------------------------------------------------


def test_pg_concurrent_job_claim_exactly_one_winner(
    pg_session: Session, pg_sessionmaker
) -> None:
    """N 个 Worker 抢 M 个作业：每个作业恰好被领一次，其余 Worker 拿到别的或 None。

    这是对 `claim_next_job` 的**真实并发**验证（两线程同时进领取事务）。
    Task 3 会把它升级为重复领取的专项断言（SKIP LOCKED 修复后这里必须继续绿）。
    """
    task = _make_task(pg_session)
    pg_session.add(
        ApprovalAttachment(
            task_id=task.id,
            attachment_id="ATT-1",
            file_name="a.pdf",
            object_key="k",
            file_checksum="0" * 64,
            download_status="success",
            content_type="application/pdf",
        )
    )
    jobs = [
        WorkflowJob(
            task_id=task.id,
            job_type=JobType.PARSE.value,
            job_status=JobStatus.QUEUED.value,
            # ⚠️ 与生产一致：作业必须有幂等键（去重唯一约束的一部分）
            idempotency_key=f"pg-smoke:{task.id}:{index}",
            input_json='{"document_id": 1}',
            # ⚠️ schema.sql 里这两列有 DEFAULT，ORM 元数据里却没有 server_default ——
            # 这正是 Task 2 迁移比对要清单化的差异（PG 下 ORM 建表不会自带默认值）
            input_digest=f"digest-{index}",
            max_attempts=3,
        )
        for index in range(2)
    ]
    pg_session.add_all(jobs)
    pg_session.commit()

    # 两个"Worker"同时抢 2 个作业：结果必须互不相同（不存在同行双领）
    def grab(worker_id: str):
        session = pg_sessionmaker()
        try:
            return claim_next_job(session, worker_id=worker_id)
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(grab, ["w-1", "w-2"]))

    claimed_ids = [r.job_id for r in results if r is not None]
    assert len(claimed_ids) == len(set(claimed_ids)), (
        f"同一作业被领取了多次：{claimed_ids}"
    )
    assert sorted(claimed_ids) == sorted(j.id for j in jobs), (
        f"应恰好领走全部 2 个作业，实际 {claimed_ids}"
    )
