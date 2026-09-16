"""PG 并发领取语义（M9 Task 3）。

计划要求的两条证明：
1. **两真并发下同一作业不能被领两次**（`SKIP LOCKED` 修复后仍成立）；
2. **租约回收对"现在"的判据一致**：过期判定与退避调度用同一个 `now`。

⚠️ 并发用**真线程 + 独立会话**，不用 monkeypatch 假并发 ——
"两个 UPDATE 是否真的在同一时刻竞争同一行"这件事，只有真并发能证明。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.enums import JobStatus, JobType
from app.models import ApprovalTask, WorkflowJob
from app.worker import claim_next_job, recycle_expired_leases
from datetime import timedelta

from app.workflow.jobs import backoff_seconds, utcnow


def _seed_jobs(session, count: int) -> list[int]:
    task = ApprovalTask(
        provider="mock",
        tenant_id="default",
        instance_id="HT-CONC",
        approval_code="HT-CONC-0001",
    )
    session.add(task)
    session.flush()
    jobs = [
        WorkflowJob(
            task_id=task.id,
            job_type=JobType.PARSE.value,
            job_status=JobStatus.QUEUED.value,
            idempotency_key=f"conc:{task.id}:{index}",
            input_json=f'{{"document_id": {index}}}',
            input_digest=f"digest-{index}",
            max_attempts=3,
        )
        for index in range(count)
    ]
    session.add_all(jobs)
    session.commit()
    return [job.id for job in jobs]


def test_pg_two_workers_never_claim_the_same_job(pg_session, pg_sessionmaker) -> None:
    """8 个 Worker 抢 8 个作业：每个作业恰好被领一次（多轮压测）。"""
    job_ids = _seed_jobs(pg_session, count=8)
    rounds = 5  # 重复跑，把"恰好同时"的概率放大

    for _ in range(rounds):
        # 每轮重置为 queued（模拟重新入队）
        for job_id in job_ids:
            session = pg_sessionmaker()
            try:
                job = session.get(WorkflowJob, job_id)
                job.job_status = JobStatus.QUEUED.value
                job.lease_owner = None
                job.lease_token = None
                job.lease_expires_at = None
                job.attempt_no = 0
                session.commit()
            finally:
                session.close()

        barrier_ready = []

        def grab(worker_id: str):
            session = pg_sessionmaker()
            try:
                return claim_next_job(
                    session,
                    worker_id=worker_id,
                    job_types=[JobType.PARSE],
                )
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(grab, [f"w-{i}" for i in range(8)]))

        claimed_ids = [r.job_id for r in results if r is not None]
        assert len(claimed_ids) == len(set(claimed_ids)), (
            f"同一作业被领取多次：{sorted(claimed_ids)}"
        )
        assert set(claimed_ids) == set(job_ids), (
            f"SKIP LOCKED 下不应有作业被漏领：{set(job_ids) - set(claimed_ids)}"
        )


def test_pg_lease_recycle_uses_one_consistent_now(pg_session) -> None:
    """租约回收：过期判定与退避调度必须用**同一个** now。

    判据写进 SQL（`lease_expires_at <= :now`），调度也用同一个 `:now` ——
    换 PG 后若有人改成"数据库时间 + 应用时间混用"，这里会算出负的退避。
    """
    task = ApprovalTask(
        provider="mock",
        tenant_id="default",
        instance_id="HT-LEASE",
        approval_code="HT-LEASE-0001",
        task_status="parsing",
    )
    pg_session.add(task)
    pg_session.flush()
    job = WorkflowJob(
        task_id=task.id,
        job_type=JobType.PARSE.value,
        job_status=JobStatus.RUNNING.value,
        idempotency_key="lease:1",
        input_json="{}",
        input_digest="d",
        max_attempts=3,
        attempt_no=1,
        lease_owner="w-dead",
        lease_token="tok",
        lease_expires_at=utcnow() - timedelta(seconds=1),  # 刚过期
    )
    pg_session.add(job)
    pg_session.commit()

    now = utcnow()
    recycled = recycle_expired_leases(pg_session, now=now)
    pg_session.commit()

    assert recycled == [job.id]
    refreshed = pg_session.get(WorkflowJob, job.id)
    assert refreshed.job_status == JobStatus.RETRY_WAIT.value
    expected = now + timedelta(seconds=backoff_seconds(1))
    # 同一个 now：退避时间与过期判定同源（允许秒级舍入）
    assert abs((refreshed.next_retry_at - expected).total_seconds()) < 1.0
