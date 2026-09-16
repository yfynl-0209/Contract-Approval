"""Worker 骨架测试（M4 / T6，设计文档 §4.4）。

本文件守住五类**不会自己报错**的失败：

1. **重复扣重试预算**：领取递增一次、回收再递增一次 —— 同一次崩溃消耗两次额度，
   `max_attempts=3` 实际只允许约 1.5 次真实重试。它不会报错，
   只会让"重试次数"变成名义值。
2. **不是 fencing 的租约**：只匹配 `worker_id` 时，**同一个 worker 重启后**
   仍能用旧租约提交成功，覆盖新执行的结果。因此关键用例必须**固定 worker_id、
   让 token 变化** —— 换 owner 测不出这个问题。
3. **回收直接置回 `queued`**：崩溃循环。刚崩掉的作业立刻被下一个 Worker 捡起，
   几秒内烧光 `max_attempts`，同时满载 CPU。
4. **业务写入与完成作业分两次提交**：越权写入的业务数据**留在库里**，
   而症状出现在**另一个** Worker 身上（撞唯一约束，报"解析失败"）。
   **症状与病因完全错位。**
5. **停机要等一个完整轮询间隔**：`time.sleep` 而不是 `event.wait`，
   表现是每次部署都要等强杀。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.context import get_correlation_id
from app.db import Base
from app.enums import ErrorCode, JobStatus, JobType, LogType
from app.errors import LeaseLost, PermanentGatewayError, TransientGatewayError
from app.models import TaskLog, WorkflowJob
from app.services.log_service import LogService
from app.worker import (
    Worker,
    claim_next_job,
    complete_job,
    extend_lease,
    recycle_expired_leases,
)
from app.workflow.jobs import create_job

T0 = datetime(2026, 9, 14, 10, 0, 0)
INPUT = {"provider": "mock", "tenant_id": "default"}


@pytest.fixture()
def engine(work_dir) -> Engine:
    import sqlalchemy as sa

    eng = sa.create_engine(f"sqlite:///{(work_dir / 'worker.db').as_posix()}", future=True)
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture()
def session(engine: Engine):
    with Session(engine) as db_session:
        yield db_session


@pytest.fixture()
def factory(engine: Engine):
    return lambda: Session(engine)


def _make_job(session: Session, key: str = "pull:v1", *, max_attempts: int = 3) -> int:
    job, _ = create_job(
        session,
        job_type=JobType.PULL,
        idempotency_key=key,
        input_payload=INPUT,
        max_attempts=max_attempts,
    )
    session.commit()
    return job.id


def _status(session: Session, job_id: int) -> WorkflowJob:
    session.expire_all()
    return session.execute(
        select(WorkflowJob).where(WorkflowJob.id == job_id)
    ).scalar_one()


# ============================================================
# 1. 原子领取
# ============================================================


def test_claim_picks_one_job_and_marks_it_running(session: Session) -> None:
    job_id = _make_job(session)

    claimed = claim_next_job(session, worker_id="w1", now=T0)

    assert claimed is not None
    assert claimed.job_id == job_id
    assert claimed.input == INPUT, "输入必须原样带出来（不可变输入的用途就在这里）"

    stored = _status(session, job_id)
    assert stored.job_status == JobStatus.RUNNING.value
    assert stored.lease_owner == "w1"
    assert stored.lease_token == claimed.lease_token
    assert stored.started_at == T0


def test_claim_increments_attempt_no_exactly_once(session: Session) -> None:
    """领取 = 开始一次执行 → 递增一次（`mark_running` 的语义）。"""
    job_id = _make_job(session)

    claim_next_job(session, worker_id="w1", now=T0)

    assert _status(session, job_id).attempt_no == 1


def test_claimed_job_is_not_claimable_again(session: Session) -> None:
    """`running` 的作业不得被第二个 Worker 领走 —— 那是重复的 OCR 推理。"""
    _make_job(session)

    first = claim_next_job(session, worker_id="w1", now=T0)
    second = claim_next_job(session, worker_id="w2", now=T0)

    assert first is not None
    assert second is None, "同一作业被领取了两次"


def test_claim_returns_none_when_queue_is_empty(session: Session) -> None:
    assert claim_next_job(session, worker_id="w1", now=T0) is None


def test_claim_generates_a_new_token_every_time(session: Session) -> None:
    """每次领取都换 token —— 判据绑定"这一次领取"，不是"哪个 Worker"。"""
    _make_job(session, "pull:a")
    _make_job(session, "pull:b")

    first = claim_next_job(session, worker_id="w1", now=T0)
    second = claim_next_job(session, worker_id="w1", now=T0)

    assert first is not None and second is not None
    assert first.lease_token != second.lease_token


def test_claim_is_committed_before_execution(session: Session, engine: Engine) -> None:
    """租约必须先落库：执行事务回滚时不得把"已领取"一起回滚掉。

    否则别的 Worker 会**重复执行**同一作业 —— 而两个执行都"成功"时，
    库里会是两条记录，`parse_version` 该算到几变成事后猜测。
    """
    _make_job(session)
    claim_next_job(session, worker_id="w1", now=T0)

    # 另一个连接（模拟另一个进程）必须已经能看到这条 running 记录
    with Session(engine) as other:
        row = other.execute(select(WorkflowJob)).scalar_one()
        assert row.job_status == JobStatus.RUNNING.value
        assert row.lease_token is not None


# ============================================================
# 2. 回收：不递增 attempt_no，且走退避
# ============================================================


def test_recycle_does_not_increment_attempt_no(session: Session) -> None:
    """**本文件最重要的一条之一**：回收不递增 `attempt_no`。

    递增的话，同一次崩溃消耗两次额度 —— `max_attempts=3` 只允许约 1.5 次真实重试，
    而"重试次数是名义值"这件事不会报错。
    """
    job_id = _make_job(session)
    claim_next_job(session, worker_id="w1", now=T0)
    assert _status(session, job_id).attempt_no == 1

    recycled = recycle_expired_leases(session, now=T0 + timedelta(minutes=10))
    session.commit()

    assert recycled == [job_id]
    assert _status(session, job_id).attempt_no == 1, "回收又扣了一次重试预算"


def test_recycle_goes_to_retry_wait_with_backoff(session: Session) -> None:
    """回收后是 `retry_wait` + 退避，**不是** `queued`。

    直接置回可执行 = 崩溃循环：刚崩掉的作业立刻被捡起，
    如果崩溃原因是这个作业本身，几秒内烧光 `max_attempts`，同时满载 CPU。
    """
    job_id = _make_job(session)
    claim_next_job(session, worker_id="w1", now=T0)

    moment = T0 + timedelta(minutes=10)
    recycle_expired_leases(session, now=moment)
    session.commit()

    stored = _status(session, job_id)
    assert stored.job_status == JobStatus.RETRY_WAIT.value
    assert stored.next_retry_at is not None, "没有退避时间，等于直接排队重跑"
    assert stored.next_retry_at > moment
    assert stored.last_error_code == ErrorCode.LEASE_LOST.value

    # 冷却期内**必须领不到** —— 这就是"不让失败变成拒绝服务"的那道闸门
    assert claim_next_job(session, worker_id="w2", now=moment) is None


def test_recycled_job_becomes_claimable_after_backoff(session: Session) -> None:
    """退避到点后必须能重新领取 —— 否则退避就变成了永久卡死。"""
    _make_job(session)
    claim_next_job(session, worker_id="w1", now=T0)

    moment = T0 + timedelta(minutes=10)
    recycle_expired_leases(session, now=moment)
    session.commit()

    later = moment + timedelta(hours=1)
    reclaimed = claim_next_job(session, worker_id="w2", now=later)

    assert reclaimed is not None


def test_recycle_marks_failed_when_attempts_exhausted(session: Session) -> None:
    job_id = _make_job(session, max_attempts=1)
    claim_next_job(session, worker_id="w1", now=T0)

    recycle_expired_leases(session, now=T0 + timedelta(minutes=10))
    session.commit()

    stored = _status(session, job_id)
    assert stored.job_status == JobStatus.FAILED.value
    assert stored.finished_at is not None


def test_recycle_ignores_jobs_with_a_live_lease(session: Session) -> None:
    _make_job(session)
    claim_next_job(session, worker_id="w1", now=T0)

    recycled = recycle_expired_leases(session, now=T0 + timedelta(seconds=1))

    assert recycled == [], "租约还活着就被回收，会把正常执行中的作业夺走"


def test_recycle_clears_the_lease(session: Session) -> None:
    """清空租约后，旧 Worker 的条件完成**一定**匹配不到。"""
    job_id = _make_job(session)
    claim_next_job(session, worker_id="w1", now=T0)

    recycle_expired_leases(session, now=T0 + timedelta(minutes=10))
    session.commit()

    stored = _status(session, job_id)
    assert stored.lease_owner is None
    assert stored.lease_token is None
    assert stored.lease_expires_at is None


# ============================================================
# 3. fencing：同一 worker_id、不同 token
# ============================================================


def test_old_token_cannot_complete_after_release(session: Session) -> None:
    """**fencing 的关键用例：固定 `worker_id`，只让 token 变化。**

    只换 `owner` 时测不出问题 —— 那正是初版的漏洞所在：
    `worker_id` 通常是稳定的（重启复用同一标识），
    于是"卡顿后恢复的旧执行"用**过期租约**覆盖了新执行的结果，
    而它看起来只是一次普通的成功完成。
    """
    job_id = _make_job(session)
    stale = claim_next_job(session, worker_id="w1", now=T0)
    assert stale is not None

    moment = T0 + timedelta(minutes=10)
    recycle_expired_leases(session, now=moment)
    session.commit()

    # 同一个 worker_id 重新领取 —— owner 相同，只有 token 变了
    fresh = claim_next_job(session, worker_id="w1", now=moment + timedelta(hours=1))
    assert fresh is not None and fresh.job_id == job_id
    assert fresh.lease_token != stale.lease_token

    with pytest.raises(LeaseLost) as excinfo:
        complete_job(session, stale, now=moment + timedelta(hours=1))

    assert excinfo.value.code is ErrorCode.LEASE_LOST
    assert excinfo.value.retryable is False


def test_complete_succeeds_while_lease_is_held(session: Session) -> None:
    job_id = _make_job(session)
    claimed = claim_next_job(session, worker_id="w1", now=T0)
    assert claimed is not None

    complete_job(session, claimed, now=T0 + timedelta(seconds=5))
    session.commit()

    stored = _status(session, job_id)
    assert stored.job_status == JobStatus.SUCCEEDED.value
    assert stored.lease_token is None
    assert stored.attempt_no == 0, "成功后必须重置尝试计数，否则成功会吃光重试预算"


def test_complete_after_lease_expiry_is_rejected(session: Session) -> None:
    """租约**自然过期**（还没被回收）时也不得完成。

    否则"慢跑的旧执行"只要赶在回收扫描之前提交，就能覆盖新执行的结果 ——
    而回收扫描是有间隔的，这个窗口是真实存在的。
    """
    _make_job(session)
    claimed = claim_next_job(session, worker_id="w1", now=T0, lease_seconds=60)
    assert claimed is not None

    with pytest.raises(LeaseLost):
        complete_job(session, claimed, now=T0 + timedelta(seconds=61))


# ============================================================
# 4. 同事务：越权写入必须一并回滚（§4.4.3）
# ============================================================


def test_business_write_rolls_back_when_lease_is_lost(
    session: Session, engine: Engine
) -> None:
    """**越权写入的业务数据不得留库。**

    留库的话，库里会有一份**没有任何作业引用它**的解析结果，
    而新 Worker 稍后写入时撞上唯一约束、报"解析失败" ——
    用户看到的是解析失败，真正的原因却在旧 Worker 的越权写入。
    **症状与病因完全错位。**
    """
    _make_job(session)
    stale = claim_next_job(session, worker_id="w1", now=T0)
    assert stale is not None

    # 另一个进程回收了租约
    with Session(engine) as other:
        recycle_expired_leases(other, now=T0 + timedelta(minutes=10))
        other.commit()

    # 旧 Worker 在自己的事务里：先写业务数据，再条件完成
    LogService(session).log(log_type=LogType.SYSTEM, message="旧 Worker 的越权写入")
    with pytest.raises(LeaseLost):
        complete_job(session, stale, now=T0 + timedelta(minutes=10))
    session.rollback()

    remaining = session.execute(select(func.count()).select_from(TaskLog)).scalar_one()
    assert remaining == 0, "越权写入被留在了库里"


def test_business_write_survives_when_lease_is_held(
    session: Session, engine: Engine
) -> None:
    """反面：持有租约时，业务写入与完成**一起**提交成功。"""
    _make_job(session)
    claimed = claim_next_job(session, worker_id="w1", now=T0)
    assert claimed is not None

    LogService(session).log(log_type=LogType.SYSTEM, message="正常写入")
    complete_job(session, claimed, now=T0 + timedelta(seconds=1))
    session.commit()

    with Session(engine) as other:
        assert other.execute(select(func.count()).select_from(TaskLog)).scalar_one() == 1


# ============================================================
# 5. 续租
# ============================================================


def test_extend_lease_pushes_the_deadline(session: Session) -> None:
    _make_job(session)
    claimed = claim_next_job(session, worker_id="w1", now=T0, lease_seconds=60)
    assert claimed is not None

    extend_lease(session, claimed, lease_seconds=600, now=T0 + timedelta(seconds=50))
    session.commit()

    stored = _status(session, claimed.job_id)
    assert stored.lease_expires_at == T0 + timedelta(seconds=650)


def test_extend_lease_raises_when_ownership_is_lost(session: Session) -> None:
    """续租失败 = 已失去所有权 → 处理器必须**立即停止并放弃写入**。

    做成异常而不是返回值，是为了让"忘了检查"不可能发生。
    """
    _make_job(session)
    claimed = claim_next_job(session, worker_id="w1", now=T0, lease_seconds=60)
    assert claimed is not None

    with pytest.raises(LeaseLost):
        extend_lease(session, claimed, now=T0 + timedelta(seconds=61))


# ============================================================
# 6. Worker 循环
# ============================================================


def _worker(factory, handler, **kwargs) -> Worker:
    return Worker(factory, handler, worker_id="w1", job_types=(JobType.PULL,), **kwargs)


def test_run_once_returns_false_when_idle(factory) -> None:
    assert _worker(factory, lambda run: None).run_once() is False


def test_successful_run_completes_the_job(session: Session, factory) -> None:
    job_id = _make_job(session)

    _worker(factory, lambda run: None).run_once()

    assert _status(session, job_id).job_status == JobStatus.SUCCEEDED.value


def test_handler_sees_the_input_and_a_working_heartbeat(session: Session, factory) -> None:
    _make_job(session)
    seen: dict = {}

    def handler(run) -> None:
        seen["input"] = run.job.input
        seen["task_id"] = run.job.task_id
        run.heartbeat()  # 长任务续租

    _worker(factory, handler).run_once()

    assert seen["input"] == INPUT
    assert seen["task_id"] is None


def test_permanent_error_fails_the_job_without_retry(session: Session, factory) -> None:
    """确定性错误**立即失败**，不浪费重试。"""
    job_id = _make_job(session)

    def handler(run) -> None:
        raise PermanentGatewayError("认证失败", code=ErrorCode.AUTH_FAILED)

    _worker(factory, handler).run_once()

    stored = _status(session, job_id)
    assert stored.job_status == JobStatus.FAILED.value
    assert stored.last_error_code == ErrorCode.AUTH_FAILED.value


def test_transient_error_schedules_a_retry(session: Session, factory) -> None:
    """瞬时错误且有剩余尝试 → `retry_wait` + 非空退避时间。"""
    job_id = _make_job(session)

    def handler(run) -> None:
        raise TransientGatewayError("超时", code=ErrorCode.APPROVAL_API_TIMEOUT)

    _worker(factory, handler).run_once()

    stored = _status(session, job_id)
    assert stored.job_status == JobStatus.RETRY_WAIT.value
    assert stored.next_retry_at is not None


def test_transient_error_after_budget_is_exhausted_fails(session: Session, factory) -> None:
    job_id = _make_job(session, max_attempts=1)

    def handler(run) -> None:
        raise TransientGatewayError("超时", code=ErrorCode.APPROVAL_API_TIMEOUT)

    _worker(factory, handler).run_once()

    assert _status(session, job_id).job_status == JobStatus.FAILED.value


def test_code_defect_is_recorded_as_unexpected_not_as_business_failure(
    session: Session, factory
) -> None:
    """处理器抛 `TypeError` 这类**代码缺陷**：记下来，但**不包装成业务错误**。

    `app/errors.py` 的分工是明确的：`AppError` 只装"可预期业务错误"。
    包装会让一个代码缺陷看起来像一次正常的业务失败，从此没人去修它。
    同时 Worker **不退出** —— 一个处理器缺陷不该让所有作业都停下。
    """
    job_id = _make_job(session)

    def handler(run) -> None:
        raise TypeError("handler bug")

    worker = _worker(factory, handler)
    assert worker.run_once() is True, "缺陷必须被记录，且 Worker 必须存活"

    stored = _status(session, job_id)
    assert stored.job_status == JobStatus.FAILED.value
    assert stored.last_error_code == ErrorCode.UNEXPECTED_ERROR.value
    assert "TypeError" in (stored.last_error_text or "")


def test_failure_is_logged_with_the_same_code(session: Session, factory) -> None:
    """日志与作业记录用**同一个码** —— 否则两边对不上，统计就分成两份。"""
    _make_job(session)

    def handler(run) -> None:
        raise PermanentGatewayError("认证失败", code=ErrorCode.AUTH_FAILED)

    _worker(factory, handler).run_once()

    log = session.execute(select(TaskLog)).scalars().one()
    assert log.log_level == "error"
    assert log.error_code == ErrorCode.AUTH_FAILED.value


def test_worker_rebinds_the_correlation_id_from_the_job(session: Session, factory) -> None:
    """Worker 的关联 ID 来自**作业记录**：contextvar 跨不过进程边界。

    不重新绑定的话，Worker 的日志与请求侧断链 —— 而两边各自都正常。
    """
    from app.context import correlation_scope

    with correlation_scope("req-abc"):
        job_id = _make_job(session)
    seen: dict = {}

    def handler(run) -> None:
        seen["correlation_id"] = get_correlation_id()

    _worker(factory, handler).run_once()

    assert seen["correlation_id"] == "req-abc"
    assert _status(session, job_id).correlation_id == "req-abc"


def test_lease_lost_is_logged_and_does_not_touch_business_data(
    session: Session, factory
) -> None:
    """处理器越权写入时：业务数据回滚，但**日志留下来**。

    日志是"越权写入被作废"的唯一证据。不留的话，
    库里只剩"作业被回收"与"新 Worker 报解析失败"，真正的原因没有任何痕迹。

    ⚠️ 这里在**同一个 session** 上把租约改掉，而不是开第二个连接。
    不是图省事：SQLite 是单写者模型，`run.session` 此时已持有未提交的写入
    （那条业务日志），第二个连接的任何写操作都会撞 `database is locked` ——
    于是异常变成 `OperationalError`，这条测试会测到**别的东西**。
    **多 Worker 并发写同一个 SQLite 库时的锁竞争是真实约束**，见 `app/worker.py`。
    """
    _make_job(session)

    def handler(run) -> None:
        LogService(run.session).log(log_type=LogType.SYSTEM, message="越权业务写入")
        # 模拟"租约已被回收/转手"：token 被改掉
        run.session.execute(
            WorkflowJob.__table__.update()
            .where(WorkflowJob.id == run.job.job_id)
            .values(lease_token="someone-else", lease_expires_at=None)
        )

    _worker(factory, handler).run_once()

    logs = session.execute(select(TaskLog)).scalars().all()
    assert [log.error_code for log in logs] == [ErrorCode.LEASE_LOST.value], (
        f"越权路径应当只留下 LEASE_LOST 的告警，实际：{[(log.error_code, log.log_content) for log in logs]}"
    )
    assert "越权业务写入" not in (logs[0].log_content or "")


# ============================================================
# 7. 优雅停机
# ============================================================


def test_stop_signal_interrupts_the_idle_wait(factory) -> None:
    """**停机不得等一个完整轮询间隔。**

    用 `time.sleep(interval)` 时，收到信号后还要空等一整轮 ——
    而编排系统通常只给几秒，于是每次部署都要等强杀。
    这里把间隔设成 30 秒：若实现是 sleep，线程不可能在 5 秒内结束。
    """
    worker = _worker(factory, lambda run: None, poll_interval=30.0)
    thread = threading.Thread(target=worker.run_forever, daemon=True)

    thread.start()
    time.sleep(0.3)  # 让它进入空闲等待
    worker.request_stop()
    thread.join(timeout=5.0)

    assert not thread.is_alive(), "停机信号没有打断空闲等待（用了 sleep 而不是 wait）"


def test_run_forever_respects_max_iterations(factory, session: Session) -> None:
    _make_job(session)

    worker = _worker(factory, lambda run: None, poll_interval=0.0)
    worker.run_forever(max_iterations=1)

    assert _status(session, 1).job_status == JobStatus.SUCCEEDED.value
