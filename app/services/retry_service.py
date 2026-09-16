"""人工重试：从**失败检查点**恢复任务（M7 / 需求 2.4.4、计划 §7.4）。

## 本文回答的三个问题

| 问题 | 答案在哪里 |
| --- | --- |
| 恢复到哪个业务状态 | `app/workflow/state_machine.py::resume_target` |
| 重新执行哪一步 | `app/workflow/state_machine.py::retry_job_type` |
| 谁在什么时候为什么重试 | `audit_events`（`TASK_RETRIED`）+ `task_logs` |

## 为什么重试矩阵必须按 `blocked_stage` 分派

`blocked_stage` 记录的是**当初卡在哪一步**，而"重试"的含义就是**把那一步重做一遍**：

| `blocked_stage` | 重跑 | 任务状态 |
| --- | --- | --- |
| `parse` | `PARSE` 作业重新入队 | `parsing` |
| `rule` | `RULE` 作业重新入队 | `reviewing` |
| `result` | `RESULT` 作业重新入队（**不重跑规则**） | `reviewing` |
| `writeback` | **只重新武装 Outbox 投递**，不碰解析与规则 | `reviewing` |

⚠️ 回写失败**不新建作业**：回写意图早就登记在 `comment_logs` + `outbox_events` 里，
失败的是**送达**。再登记一次意图没有意义（幂等键相同 → 复用旧尝试），
正确做法是把那条 Outbox 事件从 `failed` 重新置回 `pending` 并归还重试预算。

`pull` / `detail` / `download` 三种失败位置**刻意拒绝**（`RETRY_NOT_SUPPORTED`）：
它们由工具 1–3 在**同步**路径上完成，恢复入口是重跑对应工具
（重跑工具 3 成功后 `attachment_service` 会自己把任务从检查点拉回来）。
把它们硬映射成一个 Worker 永远不会领取的作业，会让任务回到 `parsing` 后**永远停住**。

## ⚠️ 先校验、再写入

`app/db.py::transactional_session` 对**业务异常也提交**（失败本身也是要留下的记录）。
因此本模块的规则是：**所有拒绝都在任何写入之前抛出**。反过来（先改一半再报错）
会在库里留下"报错了但数据已改"的记录 —— 而它看起来完全正常。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import Actor
from app.context import get_correlation_id
from app.enums import (
    AuditAction,
    ErrorCode,
    JobStatus,
    JobType,
    LogType,
    OutboxStatus,
    TaskStatus,
    WriteStatus,
)
from app.errors import InvalidStateTransition, PermanentError
from app.models import ApprovalTask, AuditEvent, CommentLog, OutboxEvent, WorkflowJob
from app.services import query_service
from app.services.log_service import LogService
from app.workflow.jobs import reset_for_retry
from app.workflow.state_machine import retry_job_type, start_retry

#: 失败位置没有可自动重跑的步骤时，告诉操作员**该去点哪个按钮**。
#:
#: ⚠️ 这段指引不是"友好的附加信息"：`RETRY_NOT_SUPPORTED` 是一个 409，
#: 而一个只说"不支持"的 409 会让操作员反复重试同一个请求。
#: 恢复入口确实存在（工具 1/2/3），把它写出来才是完整的回答。
_STAGE_GUIDANCE: dict[JobType, str] = {
    JobType.DOWNLOAD: (
        "下载失败请重新执行工具 3（download_contract_attachment）——"
        "重新下载成功后任务会自动从该检查点恢复"
    ),
    JobType.PULL: "拉取失败请重新执行工具 1（list_pending_contract_approvals）",
    JobType.DETAIL: "详情同步失败请重新执行工具 2（get_contract_approval）",
}


@dataclass(frozen=True, slots=True)
class RetryOutcome:
    """一次人工重试的结论。**字段刻意都来自库里读回的真实值**，不是推断值。"""

    task_id: int
    blocked_stage: str | None
    resumed_status: str
    retry_count: int
    reason: str
    #: `job_queued` / `writeback_rearmed` —— 这次重试**实际做了什么**。
    #: 调用方据此区分"排了一个作业"与"重新武装了一次投递"，
    #: 而不是从 `blocked_stage` 再推一遍（那会多出一份会漂移的判据）。
    action: str
    job_id: int | None = None
    job_type: str | None = None
    job_status: str | None = None
    attempt_id: int | None = None
    outbox_event_id: int | None = None


def retry_task(
    session: Session,
    *,
    tenant_id: str,
    task_id: int,
    reason: str,
    actor: Actor,
) -> RetryOutcome:
    """把一条 `blocked` 任务从失败检查点恢复，并留下审计与日志。

    Args:
        reason: **操作原因**（必填）。它回答的是"为什么现在要重试"——
            一次网络抖动的重试与一次人工排查后的重试，在事后看是两件事。

    Raises:
        PermanentError: 任务不存在 / 不属于本租户（`RESOURCE_NOT_FOUND`，404）；
            任务不在 `blocked`（`INVALID_STATE_TRANSITION`，409）；
            该失败位置没有可重跑的对象（`RETRY_NOT_SUPPORTED`，409）。
        ValueError: `reason` 为空白（→ 400）。
    """
    task = query_service.get_task(session, tenant_id=tenant_id, task_id=task_id)

    operator_reason = _require_reason(reason)

    if task.task_status != TaskStatus.BLOCKED.value:
        raise InvalidStateTransition(
            f"任务 {task_id} 当前状态为 {task.task_status!r}，只有 "
            f"{TaskStatus.BLOCKED.value!r} 的任务可以人工重试 ——"
            "非阻塞任务没有需要恢复的失败位置",
            code=ErrorCode.INVALID_STATE_TRANSITION,
        )

    stage = task.blocked_stage
    job_type = retry_job_type(stage)

    # ---- 校验阶段：以下分支**全部在任何写入之前**抛出 ----
    if stage == JobType.WRITEBACK.value:
        target = _failed_writeback_target(session, task)
    elif job_type is not None:
        target = _retryable_job(session, task, job_type)
    else:
        raise PermanentError(
            _unsupported_message(stage), code=ErrorCode.RETRY_NOT_SUPPORTED
        )

    # ---- 写入阶段：到这里为止所有拒绝都已发生 ----
    resumed = start_retry(task)

    if isinstance(target, _WritebackTarget):
        _rearm_writeback(session, task, target)
        job = None
        action = "writeback_rearmed"
    else:
        reset_for_retry(session, target)
        job = target
        action = "job_queued"

    session.add(
        AuditEvent(
            task_id=task.id,
            actor_id=actor.actor_id,
            actor_name=actor.display_name,
            action=AuditAction.TASK_RETRIED.value,
            target_type="approval_task",
            target_id=task.id,
            correlation_id=get_correlation_id(),
            detail_json=_detail_json(
                {
                    "reason": operator_reason,
                    "blocked_stage": stage,
                    "resumed_status": resumed.value,
                    "action": action,
                    "retry_count": task.retry_count,
                    "job_id": None if job is None else job.id,
                    "attempt_id": getattr(target, "attempt_id", None),
                    "outbox_event_id": getattr(target, "event_id", None),
                }
            ),
        )
    )

    LogService(session, operator=actor.display_name).log(
        log_type=LogType.SYSTEM,
        task_id=task.id,
        message=(
            f"人工重试：从检查点 {stage!r} 恢复 → {resumed.value}"
            f"（{'重排作业 ' + job.job_type if job is not None else '重新武装回写投递'}）"
            f"；原因：{operator_reason}"
        ),
        payload={
            "blocked_stage": stage,
            "resumed_status": resumed.value,
            "action": action,
            "retry_count": task.retry_count,
            "job_id": None if job is None else job.id,
        },
    )

    session.flush()

    return RetryOutcome(
        task_id=task.id,
        blocked_stage=stage,
        resumed_status=resumed.value,
        retry_count=task.retry_count or 0,
        reason=operator_reason,
        action=action,
        job_id=None if job is None else job.id,
        job_type=None if job is None else job.job_type,
        job_status=None if job is None else job.job_status,
        attempt_id=getattr(target, "attempt_id", None),
        outbox_event_id=getattr(target, "event_id", None),
    )


# ============================================================
# 校验：找一个可重跑的对象
# ============================================================


def _require_reason(reason: str) -> str:
    """操作原因必填。

    用 `ValueError` 而不是 `PermanentError`：它是**调用方的参数错误**（→400），
    而不是"业务事实"。`ValueError` 会让请求事务回滚 —— 这正是我们要的，
    因为此时**一个字段都还没写**。
    """
    text = (reason or "").strip()
    if not text:
        raise ValueError(
            "人工重试必须填写操作原因（reason）：一次「抖动后重试」与一次"
            "「排查后重试」在事后看是两件事，而审计账只记了「有人点了重试」时，"
            "没人回答得了「当时为什么要重试」"
        )
    return text


@dataclass(frozen=True, slots=True)
class _WritebackTarget:
    """回写重试的目标：要重新武装的尝试与事件。"""

    attempt: CommentLog
    event: OutboxEvent

    @property
    def attempt_id(self) -> int:
        return self.attempt.id

    @property
    def event_id(self) -> int:
        return self.event.id


def _retryable_job(
    session: Session, task: ApprovalTask, job_type: JobType
) -> WorkflowJob:
    """取该任务上**最近一个该类型**的作业，用于重排。

    Raises:
        PermanentError: 没有该类型的作业（`RETRY_NOT_SUPPORTED`，409）——
            重试沿用**当初那份冻结输入**，没有作业就没有可沿用的输入，
            凭空造一份等于让"这份结果基于什么输入"失去答案。
        PermanentError: 最近一个作业仍在执行中（`INVALID_STATE_TRANSITION`，409）——
            改一个 Worker 正持有租约的作业，会让它的完成写入与状态对不上。
    """
    job = session.execute(
        select(WorkflowJob)
        .where(
            WorkflowJob.task_id == task.id,
            WorkflowJob.job_type == job_type.value,
        )
        .order_by(WorkflowJob.id.desc())
        .limit(1)
    ).scalar_one_or_none()

    if job is None:
        raise PermanentError(
            f"任务 {task.id} 上没有 {job_type.value} 类型的作业，无法沿用冻结输入重跑："
            "重试沿用**当初那份输入**，凭空造一份会让"
            "「这份结果基于什么输入」失去答案。"
            f"{_unsupported_message(task.blocked_stage)}",
            code=ErrorCode.RETRY_NOT_SUPPORTED,
        )

    if job.job_status == JobStatus.RUNNING.value:
        raise InvalidStateTransition(
            f"任务 {task.id} 的 {job_type.value} 作业 {job.id} 仍在执行中，"
            "不能重排：它当前持有的租约会让这次重排与它的完成写入互相覆盖",
            code=ErrorCode.INVALID_STATE_TRANSITION,
        )

    return job


def _failed_writeback_target(
    session: Session, task: ApprovalTask
) -> _WritebackTarget:
    """取最近一次**失败**的回写尝试与它的 Outbox 事件。

    Raises:
        PermanentError: 没有失败的回写尝试，或那次尝试没有对应的 Outbox 事件
            （`RETRY_NOT_SUPPORTED`，409）—— 没有待送达的意图可重新武装。
    """
    attempt = session.execute(
        select(CommentLog)
        .where(
            CommentLog.task_id == task.id,
            CommentLog.write_status == WriteStatus.FAILED.value,
        )
        .order_by(CommentLog.attempt_no.desc(), CommentLog.id.desc())
        .limit(1)
    ).scalar_one_or_none()

    if attempt is None:
        raise PermanentError(
            f"任务 {task.id} 上没有失败的回写尝试，没有待送达的意图可以重新武装。"
            "回写重试的对象是**已登记但送达失败**的意图，而不是一次新的回写 ——"
            "要发起新的回写请执行工具 7（write_approval_comment）",
            code=ErrorCode.RETRY_NOT_SUPPORTED,
        )

    event = session.execute(
        select(OutboxEvent).where(
            OutboxEvent.idempotency_key == attempt.idempotency_key
        )
    ).scalar_one_or_none()

    if event is None:
        raise PermanentError(
            f"回写尝试 {attempt.id} 没有对应的 Outbox 事件，无法重新武装投递："
            "没有待送达的意图（可能它当初是被门禁拒绝的，那种情况应重新执行工具 7）",
            code=ErrorCode.RETRY_NOT_SUPPORTED,
        )

    return _WritebackTarget(attempt=attempt, event=event)


def _rearm_writeback(
    session: Session, task: ApprovalTask, target: _WritebackTarget
) -> None:
    """把一次失败的投递重新置回 `pending`，并归还**完整**重试预算。

    ⚠️ 归还预算是必须的：耗尽检测是 `attempt_no >= max_attempts`，
    不归零的话重新武装出来的事件会**立刻**被判定为再次耗尽，
    然后再次 `failed` + 再阻塞一次 —— 人工重试看起来生效了，实际什么都没变。
    """
    event = target.event
    # ⚠️ 这里是 `OutboxStatus`，**不是** `WriteStatus` —— 两者都叫"状态"，
    # 取值域却完全不同（`pending` 只在前者里）。写错时数据库 CHECK 会拦住，
    # 但那时报的是「约束冲突」，与「枚举用错了」看不出关联。
    event.event_status = OutboxStatus.PENDING.value
    event.attempt_no = 0
    event.next_retry_at = None
    event.lease_owner = None
    event.lease_expires_at = None
    event.last_error_code = None
    event.last_error_text = None
    # CHECK `event_status = 'delivered' OR delivered_at IS NULL` 要求它为空；
    # 失败的事件本来就没有送达时间，显式清一遍是为了让这条不变式在这里**看得见**。
    event.delivered_at = None

    attempt = target.attempt
    # 尝试回到"已登记、待送达"：失败原因已由审计与日志留痕，
    # 留着一个 `failed` 的原因与 `writing` 的状态并排只会自相矛盾。
    attempt.write_status = WriteStatus.WRITING.value
    attempt.reason_code = None
    attempt.reason_text = None

    task.write_status = WriteStatus.WRITING.value


def _unsupported_message(blocked_stage: str | None) -> str:
    """给出"该去执行哪个工具"的指引，而不是只说"不支持"。"""
    try:
        stage = JobType(blocked_stage) if blocked_stage is not None else None
    except ValueError:
        stage = None

    if stage is not None and stage in _STAGE_GUIDANCE:
        return f"失败位置 {blocked_stage!r} 的重试入口不在本接口：{_STAGE_GUIDANCE[stage]}"

    return (
        f"任务的失败位置为 {blocked_stage!r}，本接口没有可重跑的步骤 ——"
        "它由调用端同步完成，恢复入口是重新执行对应的工具"
        "（拉取 → 工具 1，详情 → 工具 2，下载 → 工具 3）"
    )


def _detail_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)
