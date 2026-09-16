"""Worker 骨架 —— 原子领取、租约（fencing）、条件完成与优雅停机（设计文档 §4.4）。

## 三条不变量，每条都对应一类**不会报错**的缺陷

**① 领取必须是原子的。**
"先无条件 SELECT 再 UPDATE" 会让两个 Worker 同时看到同一个 `queued` 作业，
然后**都去执行**。重复执行最贵的动作是重复的 OCR 推理 ——
两次都"成功"，库里两条记录，`parse_version` 该算到几变成事后猜测。

**② 完成必须同时匹配 `lease_owner` 与 `lease_token`**（§4.4.2）。
只匹配 `owner` **不是** fencing：`worker_id` 通常是稳定的（重启复用同一标识），
于是卡顿后恢复的旧执行仍会通过校验，用**过期租约覆盖新 Worker 的结果**，
而它看起来只是一次普通的成功完成。

**③ 回收租约**不递增** `attempt_no`**（§4.4.1）。
领取时已经递增过一次（`mark_running` 的语义），回收再递增就是
**同一次崩溃消耗两次额度** —— `max_attempts=3` 实际只允许约 1.5 次真实重试，
而"重试次数"变成名义值这件事**不会报错**。

## 事务边界（§4.4.3）

| 阶段 | 事务 | 理由 |
| --- | --- | --- |
| 领取 | **独立提交** | 租约必须**先于**执行落库。与执行同一个事务时，执行失败回滚会把"已领取"也回滚掉，别的 Worker 于是重复执行同一作业 |
| 执行 | 一个事务 | 业务写入与**条件完成**一起提交。分两次提交时，"数据写了、状态没写"会留下一份没有任何作业引用它的业务数据 |
| 记录失败 | **独立事务** | 上面那个事务已经被回滚了 |

## 优雅停机

`run_forever` 用 `stop_event.wait(interval)` 而不是 `time.sleep(interval)` ——
后者的表现是"收到停机信号后还要等一个完整轮询间隔才退出"，
而编排系统通常只给几秒。

## ⚠️ M9 迁移 PostgreSQL 时必须改掉的两处（与 §0.7 同类：本机正常，换库才暴露）

1. **领取不是 `SKIP LOCKED`**：`UPDATE ... WHERE id = (SELECT ... LIMIT 1)`
   在 SQLite 下由写锁串行化，因此可靠；PG 下两个并发事务可能选中**同一行**，
   需要 `SELECT ... FOR UPDATE SKIP LOCKED`。
2. **SQLite 是单写者**：执行事务期间持有写锁，另一个 Worker（或回收扫描）
   的写入会撞 `database is locked`。表现是"偶发失败"且与负载相关。
   PG 的 MVCC 没有这个问题。在此之前，**执行事务必须短** ——
   这也正是"每处理完一页就续租"的另一层理由：它天然把工作切成了多个短事务。
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from app.context import correlation_scope
from app.enums import ErrorCode, JobStatus, JobType, LogLevel, LogType
from app.errors import AppError, LeaseLost
from app.models import WorkflowJob
from app.services.log_service import LogService
from app.workflow.jobs import backoff_seconds, mark_failed, utcnow

#: 租约时长（秒）。长任务靠 `heartbeat()` 续租，因此它只需覆盖"两个心跳之间"。
DEFAULT_LEASE_SECONDS = 120.0
#: 轮询间隔（秒）
DEFAULT_POLL_INTERVAL = 1.0


@dataclass(frozen=True)
class ClaimedJob:
    """一次领取的快照 —— 执行期间**不要**回读数据库取这些值。

    回读会拿到被回收/被别人改写后的值，于是"我以为我持有租约"这件事
    就建立在一个会变的基础上。快照是本次执行的**凭据**。
    """

    job_id: int
    job_type: str
    task_id: int | None
    input: dict[str, Any]
    checkpoint: dict[str, Any] | None
    correlation_id: str | None
    lease_token: str
    attempt_no: int
    max_attempts: int

    @property
    def attempts_left(self) -> int:
        return max(0, self.max_attempts - self.attempt_no)


@dataclass
class JobRun:
    """交给处理器的执行上下文。"""

    session: Session
    job: ClaimedJob
    #: 续租。长任务（解析 50 页扫描件）每处理完一页调一次。
    #: **它在失去所有权时会抛 `LeaseLost`** —— 处理器不必自己判断，
    #: 也就不会出现"忘了判断"这种漏法。
    heartbeat: Callable[[], None]


Handler = Callable[[JobRun], None]


# ============================================================
# 领取
# ============================================================


def _claimable(job_types: Sequence[JobType], now: datetime) -> tuple:
    """可领取的条件：类型匹配，且（排队中 或 退避到点）。

    ⚠️ `next_retry_at IS NULL` 也视为可领取：`retry_wait` 却没有下次时间
    （只可能来自手工改库或旧数据）如果不放行，那个作业会**永远卡住**，
    而看板上它只是"等待中"。宁可早跑一次，也不要静默卡死。
    """
    types = [item.value if isinstance(item, JobType) else item for item in job_types]
    return (
        WorkflowJob.job_type.in_(types),
        or_(
            WorkflowJob.job_status == JobStatus.QUEUED.value,
            and_(
                WorkflowJob.job_status == JobStatus.RETRY_WAIT.value,
                or_(
                    WorkflowJob.next_retry_at.is_(None),
                    WorkflowJob.next_retry_at <= now,
                ),
            ),
        ),
    )


def claim_next_job(
    session: Session,
    *,
    job_types: Sequence[JobType] = tuple(JobType),
    worker_id: str,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
    now: datetime | None = None,
) -> ClaimedJob | None:
    """回收过期租约，然后**原子领取**一个作业；没有可领的返回 `None`。

    ⚠️ **本函数会提交**（领取必须独立于执行落库，见模块说明）。

    ⚠️ **PG 语义（M9 已落）**：候选子查询带 `FOR UPDATE SKIP LOCKED` ——
    两个并发 Worker 不再读到同一个候选 id：后来者**跳过**被前者锁住的行，
    直接拿下一行，而不是排在锁上等它提交后再白白失败一次。
    SQLite 方言**不渲染** `FOR UPDATE`（写锁天然串行化），因此这是
    单一路径、两库通用，不存在"SQLite 走旧路径"的分叉。
    """
    moment = now or utcnow()
    recycle_expired_leases(session, now=moment)

    claimable = _claimable(job_types, moment)
    candidate = (
        select(WorkflowJob.id)
        .where(*claimable)
        .order_by(WorkflowJob.id)
        .limit(1)
        # PG：跳过被并发领取者锁住的行（M9）。SQLite：忽略，语义不变。
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )

    # 【fencing】每次领取都换一个 token：判据绑定"**这一次领取**"，
    # 而不是"哪个 Worker"（理由见模块说明 ②）。
    token = uuid.uuid4().hex

    result = session.execute(
        update(WorkflowJob)
        # ⚠️ 外层 WHERE **必须重复一遍可领取条件**（不能只按 id 更新）。
        #
        # 子查询选出的候选 id 是**读取那一刻**的事实：两个并发的领取者可能
        # 读到同一个 id。甲先 UPDATE 并提交（该行变成 `running`）；
        # 乙的 UPDATE 若只按 id 匹配，会在提交后**再写一次那一行** ——
        # 两次领取都"成功"，于是**同一个作业被跑了两遍**（重复的 OCR）。
        #
        # 加上条件后，乙重算时看到 `job_status = 'running'`，匹配不上 → 0 行
        # → 本次领取失败，稍后重试。这是"条件更新"而不是"读后写"的差别，
        # 也正是 §4.4 要求"单条 UPDATE"的原因。
        .where(WorkflowJob.id == candidate, *claimable)
        .values(
            job_status=JobStatus.RUNNING.value,
            # 与 `mark_running()` 同一语义：**在开始时**递增，
            # 这样"正在跑"的作业也持有准确的尝试次数。
            attempt_no=WorkflowJob.attempt_no + 1,
            started_at=moment,
            next_retry_at=None,
            lease_owner=worker_id,
            lease_token=token,
            lease_expires_at=moment + timedelta(seconds=lease_seconds),
        )
    )
    if result.rowcount == 0:
        session.commit()
        return None

    job = session.execute(
        select(WorkflowJob).where(WorkflowJob.lease_token == token)
    ).scalar_one()
    claimed = _snapshot(job)
    # 租约先落库：执行失败回滚时**不会**把"已领取"一起回滚掉，
    # 否则别的 Worker 会重复执行同一作业。
    session.commit()
    return claimed


def _snapshot(job: WorkflowJob) -> ClaimedJob:
    return ClaimedJob(
        job_id=job.id,
        job_type=job.job_type,
        task_id=job.task_id,
        input=json.loads(job.input_json),
        checkpoint=json.loads(job.checkpoint_json) if job.checkpoint_json else None,
        correlation_id=job.correlation_id,
        lease_token=job.lease_token or "",
        attempt_no=job.attempt_no,
        max_attempts=job.max_attempts,
    )


def recycle_expired_leases(session: Session, *, now: datetime | None = None) -> list[int]:
    """回收租约已过期的 `running` 作业，返回被回收的作业 id。

    ⚠️ **不递增 `attempt_no`**（§4.4.1）：它不是一次新的执行，
    而是"上一次执行失联"这一事实被记录。递增会让同一次崩溃消耗两次额度。

    回收后走 **`retry_wait` + 退避**，**不是** `queued`：
    直接置回可执行意味着"刚崩掉的作业立刻被下一个 Worker 捡起"。
    如果崩溃原因就是这个作业本身（超大像素、触发引擎 bug 的损坏文件），
    会形成**崩溃循环** —— 几秒内烧光 `max_attempts`，同时满载 CPU。
    退避给出的冷却期不是为了等待，而是为了**不让失败变成拒绝服务**。
    """
    moment = now or utcnow()
    expired = (
        session.execute(
            select(WorkflowJob).where(
                WorkflowJob.job_status == JobStatus.RUNNING.value,
                WorkflowJob.lease_expires_at.is_not(None),
                WorkflowJob.lease_expires_at <= moment,
            )
        )
        .scalars()
        .all()
    )

    recycled: list[int] = []
    for job in expired:
        attempts_left = (job.attempt_no or 0) < (job.max_attempts or 1)
        if attempts_left:
            job.job_status = JobStatus.RETRY_WAIT.value
            job.next_retry_at = moment + timedelta(
                seconds=backoff_seconds(job.attempt_no or 1)
            )
        else:
            job.job_status = JobStatus.FAILED.value
            job.next_retry_at = None
            job.finished_at = moment

        job.last_error_code = ErrorCode.LEASE_LOST.value
        job.last_error_text = "租约过期，执行失联"
        # 清空租约：旧 Worker 的条件完成于是**一定**匹配不到，
        # 这是比"靠状态判断"更强的一道信号。
        job.lease_owner = None
        job.lease_token = None
        job.lease_expires_at = None
        recycled.append(job.id)

    return recycled


# ============================================================
# 续租与完成
# ============================================================


def _owned(claimed: ClaimedJob, now: datetime) -> tuple:
    """"本次执行仍持有租约"的**唯一判据**（§4.4.2）。"""
    return (
        WorkflowJob.id == claimed.job_id,
        WorkflowJob.lease_owner.is_not(None),
        WorkflowJob.lease_token == claimed.lease_token,
        WorkflowJob.job_status == JobStatus.RUNNING.value,
        WorkflowJob.lease_expires_at.is_not(None),
        WorkflowJob.lease_expires_at > now,
    )


def extend_lease(
    session: Session,
    claimed: ClaimedJob,
    *,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
    now: datetime | None = None,
) -> None:
    """续租（同样带 `lease_token` 条件）。**不提交**。

    Raises:
        LeaseLost: 影响行数为 0 —— 已失去所有权，处理器必须**立即停止并放弃写入**。
    """
    moment = now or utcnow()
    result = session.execute(
        update(WorkflowJob)
        .where(*_owned(claimed, moment))
        .values(lease_expires_at=moment + timedelta(seconds=lease_seconds))
    )
    if result.rowcount == 0:
        raise LeaseLost(
            f"作业 {claimed.job_id} 续租失败：租约已不属于本次执行（token={claimed.lease_token[:8]}…）",
            code=ErrorCode.LEASE_LOST,
        )


def complete_job(
    session: Session,
    claimed: ClaimedJob,
    *,
    checkpoint: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> None:
    """带租约条件的**成功**完成。**不提交**。

    ⚠️ 必须与业务写入在**同一事务**里（§4.4.3）：调用方把业务写入与本次调用
    放在同一个 session 事务中，最后一次性提交。分开提交时会出现
    "业务数据写了、作业状态没写" —— 库里留下一份**没有任何作业引用它**的结果，
    而症状会出现在**另一个** Worker 身上（它写入时撞唯一约束，报"解析失败"）。
    **症状与病因完全错位。**

    Raises:
        LeaseLost: 影响行数为 0 —— 必须**回滚整个事务**（含已写入的业务数据）。
    """
    moment = now or utcnow()
    values: dict[str, Any] = {
        "job_status": JobStatus.SUCCEEDED.value,
        "finished_at": moment,
        "lease_owner": None,
        "lease_token": None,
        "lease_expires_at": None,
        "next_retry_at": None,
        # 与 `mark_succeeded()` 一致：成功后**重置尝试计数**。
        # 不重置的话，同一窗口内成功 3 次的作业会在随后**第一次**瞬时失败时
        # 撞上 max_attempts —— 重试预算被成功的执行吃光。
        "attempt_no": 0,
        "last_error_code": None,
        "last_error_text": None,
    }
    if checkpoint is not None:
        values["checkpoint_json"] = json.dumps(checkpoint, ensure_ascii=False, default=str)

    result = session.execute(
        update(WorkflowJob).where(*_owned(claimed, moment)).values(**values)
    )
    if result.rowcount == 0:
        raise LeaseLost(
            f"作业 {claimed.job_id} 完成失败：租约已不属于本次执行，"
            "本次结果作废（业务写入必须一并回滚）",
            code=ErrorCode.LEASE_LOST,
        )


def fail_job(
    session: Session,
    claimed: ClaimedJob,
    *,
    error: AppError | None = None,
    error_code: ErrorCode | None = None,
    message: str = "",
    now: datetime | None = None,
) -> JobStatus | None:
    """记录失败并按可重试性决定 `retry_wait` 还是 `failed`。**会提交**（独立事务）。

    调用它之前，执行事务必须已经**回滚** —— 业务写入不能与失败记录一起提交。

    ⚠️ 只在**仍持有租约**时记录。租约已被回收时不再改动作业：
    那时"该重试还是该失败"应该由**回收逻辑与新的持有者**决定，
    旧 Worker 再写一次会把它们的判断覆盖掉。

    Returns:
        流转后的状态；已失去租约时返回 `None`。
    """
    moment = now or utcnow()
    job = session.execute(
        select(WorkflowJob).where(WorkflowJob.id == claimed.job_id)
    ).scalar_one_or_none()
    if job is None:
        return None
    if job.lease_token != claimed.lease_token or job.job_status != JobStatus.RUNNING.value:
        return None

    status = mark_failed(
        session, job, error=error, error_code=error_code, message=message, now=moment
    )
    job.lease_owner = None
    job.lease_token = None
    job.lease_expires_at = None
    session.commit()
    return status


# ============================================================
# Worker
# ============================================================


#: 作业类型 → 日志类型（`workflow_jobs.job_type` → `task_logs.log_type`）。
#:
#: ⚠️ **新增作业类型时必须在这里登记**：原先只判了 `PARSE`，其余全落 `SYSTEM` ——
#: 于是 `RULE` 作业的日志混进系统日志里。排查时"审查为什么没出结论"与
#: "服务本身出了什么事"是两种完全不同的问题，混在一个 `log_type` 下就只能靠猜。
_LOG_TYPES = {
    JobType.PULL.value: LogType.PULL,
    JobType.DETAIL.value: LogType.DETAIL,
    JobType.DOWNLOAD.value: LogType.DOWNLOAD,
    JobType.PARSE.value: LogType.PARSE,
    JobType.RULE.value: LogType.RULE,
    JobType.RESULT.value: LogType.RESULT,
    JobType.WRITEBACK.value: LogType.WRITEBACK,
}


def _log_type_of(job_type: str) -> LogType:
    """作业类型 → 日志类型。**未登记的类型落 `SYSTEM`**（兜底，不是默认值）。"""
    return _LOG_TYPES.get(job_type, LogType.SYSTEM)


class Worker:
    """轮询领取并执行作业。

    Args:
        session_factory: 每次调用返回一个**新的** `Session`（不得共享：
                        Session 不是线程安全的，且跨作业复用会把上一个作业
                        的未提交状态带进来）。
        handler: 作业处理器 —— 在**执行事务**内被调用，业务写入用
            `run.session`，需要保命时长任务调 `run.heartbeat()`。
        worker_id: 租约持有者标识。缺省生成一个**带随机后缀**的：
            固定标识会让"重启后的旧执行"与新执行无法区分。
        job_types: 领取哪些类型的作业。
    """

    def __init__(
        self,
        session_factory: Callable[[], Session],
        handler: Handler,
        *,
        worker_id: str | None = None,
        job_types: Sequence[JobType] = tuple(JobType),
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> None:
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self._session_factory = session_factory
        self._handler = handler
        self._job_types = tuple(job_types)
        self._lease_seconds = lease_seconds
        self._poll_interval = poll_interval
        self._stop = threading.Event()

    # ---------- 生命周期 ----------

    def request_stop(self) -> None:
        """请求停机。**不会中断正在执行的作业** —— 它跑完当前这一步才退出。"""
        self._stop.set()

    def run_forever(self, *, max_iterations: int | None = None) -> None:
        """轮询直到收到停机信号。

        ⚠️ 空闲等待用 `stop.wait()` 而不是 `time.sleep()`：
        后者的表现是"收到停机信号后还要等一个完整轮询间隔才退出"，
        而编排系统通常只给几秒 —— 于是每次部署都要等强杀。
        """
        iterations = 0
        while not self._stop.is_set():
            self.run_once()
            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                return
            if not self._stop.is_set():
                self._stop.wait(self._poll_interval)

    # ---------- 单轮 ----------

    def run_once(self) -> bool:
        """领取并执行一个作业。返回是否真的做了事（便于测试与空转判断）。"""
        session = self._session_factory()
        try:
            claimed = claim_next_job(
                session,
                job_types=self._job_types,
                worker_id=self.worker_id,
                lease_seconds=self._lease_seconds,
            )
            if claimed is None:
                return False
            self._execute(session, claimed)
            return True
        finally:
            session.close()

    def _execute(self, session: Session, claimed: ClaimedJob) -> None:
        """在**一个事务**里：处理器写入 → 条件完成 → 一次提交（§4.4.3）。

        ⚠️ Worker 侧的关联 ID 来自**作业记录**：进程内的 contextvar 跨不过进程边界，
        请求侧的 ID 只能靠 `workflow_jobs.correlation_id` 带过来。
        不重新绑定的话，Worker 的日志与请求侧断链 —— 而两边各自都正常。
        """
        run = JobRun(
            session=session,
            job=claimed,
            heartbeat=lambda: extend_lease(
                session, claimed, lease_seconds=self._lease_seconds
            ),
        )
        try:
            with correlation_scope(claimed.correlation_id):
                self._handler(run)
                complete_job(session, claimed)
            session.commit()
        except LeaseLost:
            # 业务写入必须一并回滚：否则库里留下一份没有任何作业引用它的结果，
            # 而症状会出现在**另一个** Worker 身上（见 `LeaseLost` 的说明）。
            session.rollback()
            self._log(
                session,
                claimed,
                level="warning",
                message=f"作业 {claimed.job_id} 的租约已失去，本次结果已作废",
                error_code=ErrorCode.LEASE_LOST,
            )
            # ⚠️ 必须提交：这行日志是"越权写入被作废"的**唯一证据**。
            # 不提交的话，库里只剩"作业被回收"和"新 Worker 报解析失败"两条线索，
            # 而真正的原因（旧 Worker 越权写入）没有任何痕迹。
            session.commit()
        except Exception as exc:  # noqa: BLE001 - 作业失败必须被记录，不能穿透
            session.rollback()
            self._record_failure(session, claimed, exc)

    def _record_failure(self, session: Session, claimed: ClaimedJob, exc: Exception) -> None:
        """记录失败。**不把代码缺陷包装成业务错误。**

        `app/errors.py` 的分工很明确：`AppError` 是"**可预期**业务错误"，
        `TypeError` / `KeyError` 那类**不属于**它 —— 包装会让一个代码缺陷
        看起来像一次正常的业务失败，从此没人去修它。

        因此两条路分开：
        - `AppError` → 原样交给 `mark_failed`，由**它的错误码**决定重试还是失败；
        - 其他异常 → 记 `UNEXPECTED_ERROR`（确定性），异常类型进日志。

        Worker **不重新抛出**：一个处理器缺陷不该让整个 Worker 进程退出，
        否则一次代码问题会变成"所有作业都停了"。代价是它需要靠
        `UNEXPECTED_ERROR` 这个码在日志与看板里被注意到。
        """
        if isinstance(exc, AppError):
            status = fail_job(session, claimed, error=exc)
        else:
            status = fail_job(
                session,
                claimed,
                error_code=ErrorCode.UNEXPECTED_ERROR,
                message=f"处理器抛出非业务异常 {type(exc).__name__}: {exc}",
            )

        if status is None:
            # 租约已被回收 —— 不再改动作业，"重试还是失败"由回收逻辑与新持有者决定
            self._log(
                session,
                claimed,
                level="warning",
                message=f"作业 {claimed.job_id} 失败，但租约已被回收，不再改写状态",
                error_code=ErrorCode.LEASE_LOST,
            )
            session.commit()
            return

        code = exc.code if isinstance(exc, AppError) else ErrorCode.UNEXPECTED_ERROR
        self._log(
            session,
            claimed,
            level="error",
            message=(
                f"作业 {claimed.job_id}（{claimed.job_type}）失败 → {status.value}"
                f"（剩余尝试 {claimed.attempts_left}）"
            ),
            error_code=code,
        )
        session.commit()

    def _log(
        self,
        session: Session,
        claimed: ClaimedJob,
        *,
        level: str,
        message: str,
        error_code: ErrorCode,
    ) -> None:
        LogService(session).log(
            log_type=_log_type_of(claimed.job_type),
            task_id=claimed.task_id,
            level=LogLevel(level),
            message=message,
            error_code=error_code,
            payload={"job_type": claimed.job_type, "attempt_no": claimed.attempt_no},
        )
