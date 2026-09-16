"""Outbox 派发器（M6 / Task 5）—— 把"事务性意图"送达外部审批系统。

## 为什么需要派发器

`request_writeback` 只在业务事务里留下**意图**（`outbox_events.pending`），
外部调用不可与本地事务原子提交。派发器独立轮询本表，完成外部调用并
回填结果：意图与业务状态同生共死，送达由这里保证**至少一次**，配合
幂等键达到**恰好一次**。

## 恰好一次的三道防线

1. **租约互斥**：领取不改状态（仍是 `pending`），靠 `lease_owner` /
   `lease_expires_at` 互斥 —— 状态机里没有 `dispatching` 这种
   "已领取但进程已死"需要对账的中间态；失联的租约由
   `recycle_expired_leases` 回收。
2. **超时对账**：外部调用超时不等于没写成功。上一轮以
   `APPROVAL_API_TIMEOUT` 失败的事件，重试时**先查** `get_write_result`，
   查到就直接按成功记账，**不再重发**。
3. **幂等键去重**：真正的重发（对账查不到、或死亡窗口后接管）由
   网关按幂等键去重 —— 同一键只产生一条外部评论。

## 失败语义

- 瞬时失败：退避重试（`backoff_seconds`，封顶），事件保持 `pending`，
  尝试与任务保持 `writing`。
- 重试耗尽（或确定性失败）：事件与尝试 `failed`、任务
  `blocked` 在 `writeback` —— 恢复点回 `reviewing`，**不重跑解析与规则**
  （重新审查会产生新批次，把一次写入失败放大）。

⚠️ 未知事件类型必须**拒绝**（`failed` + `UNKNOWN_EVENT_TYPE`），而不是
标记送达："跳过"会让事件静默丢失，且丢失方式在统计上看不出来。
"""

from __future__ import annotations

import json
import random
import string
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from sqlalchemy import or_, select, update

from app.context import correlation_scope
from app.enums import (
    AuditAction,
    ErrorCode,
    JobType,
    LogType,
    LogLevel,
    OutboxEventType,
    OutboxStatus,
    TaskStatus,
    WriteStatus,
)
from app.errors import AppError, PermanentError, TransientError
from app.models import ApprovalTask, AuditEvent, CommentLog, OutboxEvent
from app.ports.approval_gateway import (
    ApprovalCommentGateway,
    WriteCommentResultDTO,
)
from app.services.log_service import LogService
from app.services.result_service import ResultInputError, get_result_view
from app.workflow.jobs import backoff_seconds
from app.workflow.state_machine import can_transition, mark_blocked, transition, utcnow

#: 默认租约时长（秒）。租约 = "我领到了，正在送"；到期即视为失联。
DEFAULT_LEASE_SECONDS = 120.0

#: 空闲轮询间隔（秒）
DEFAULT_POLL_INTERVAL = 1.0

#: 派发器拒绝未知事件时写入 `last_error_code` 的稳定标识
UNKNOWN_EVENT_TYPE = "UNKNOWN_EVENT_TYPE"


@dataclass(frozen=True)
class ClaimedEvent:
    """被领取的事件快照（领取即自增 `attempt_no`）。"""

    event_id: int
    aggregate_type: str
    aggregate_id: int
    event_type: str
    payload: dict[str, Any]
    idempotency_key: str
    correlation_id: str | None
    attempt_no: int
    max_attempts: int
    #: 上一轮尝试留下的错误码 —— 超时对账的判据
    last_error_code: str | None


def recycle_expired_leases(
    session, *, now: datetime | None = None
) -> list[int]:
    """回收失联租约：到期未归还的事件解除占用，回到可领取状态。

    不自增 `attempt_no`：领取时才计数（死亡的那次由下一次领取补记）。
    """
    moment = now or utcnow()
    expired_ids = list(
        session.execute(
            select(OutboxEvent.id).where(
                OutboxEvent.event_status == OutboxStatus.PENDING.value,
                OutboxEvent.lease_owner.is_not(None),
                OutboxEvent.lease_expires_at <= moment,
            )
        ).scalars()
    )
    if expired_ids:
        session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id.in_(expired_ids))
            .values(lease_owner=None, lease_expires_at=None)
        )
    session.commit()
    return expired_ids


def claim_next_event(
    session,
    *,
    dispatcher_id: str,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
    now: datetime | None = None,
) -> ClaimedEvent | None:
    """原子领取下一个到期事件（FIFO），领取即自增 `attempt_no` 并上租约。

    独立提交：租约必须在**外部调用之前**落库 —— 否则两个派发器
    会同时送同一份内容，幂等只能靠网关兜底。
    """
    moment = now or utcnow()

    ready = (
        OutboxEvent.event_status == OutboxStatus.PENDING.value,
        or_(
            OutboxEvent.next_retry_at.is_(None),
            OutboxEvent.next_retry_at <= moment,
        ),
        # 排除已被有效租约占用的事件（未到期 = 有人在送）：
        # 领取的互斥不能只靠"谁先 UPDATE"，还得让后来者**看不见**在送的事件
        or_(
            OutboxEvent.lease_owner.is_(None),
            OutboxEvent.lease_expires_at.is_(None),
            OutboxEvent.lease_expires_at <= moment,
        ),
    )
    candidate = session.execute(
        select(OutboxEvent.id)
        .where(*ready)
        .order_by(OutboxEvent.id)
        .limit(1)
        # PG（M9）：跳过被并发派发器锁住的行，避免"等锁 → 失败 → 白转一圈"；
        # SQLite 方言不渲染 FOR UPDATE，语义不变。
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()
    if candidate is None:
        session.commit()
        return None

    rowcount = session.execute(
        update(OutboxEvent)
        .where(
            OutboxEvent.id == candidate,
            *ready,
        )
        .values(
            lease_owner=dispatcher_id,
            lease_expires_at=moment + timedelta(seconds=lease_seconds),
            attempt_no=OutboxEvent.attempt_no + 1,
        )
    ).rowcount
    session.commit()

    if rowcount == 0:
        # 被并发抢走：本轮放弃（下一轮再领别的事件）
        return None

    event = session.get(OutboxEvent, candidate)
    assert event is not None  # 刚刚更新成功，行必然存在
    try:
        payload = json.loads(event.payload_json)
    except (TypeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return ClaimedEvent(
        event_id=event.id,
        aggregate_type=event.aggregate_type,
        aggregate_id=event.aggregate_id,
        event_type=event.event_type,
        payload=payload,
        idempotency_key=event.idempotency_key,
        correlation_id=event.correlation_id,
        attempt_no=event.attempt_no,
        max_attempts=event.max_attempts,
        last_error_code=event.last_error_code,
    )


class OutboxDispatcher:
    """轮询 `outbox_events` 并送达的常驻派发器（对齐 `Worker` 的模式）。"""

    def __init__(
        self,
        session_factory: Callable[[], Any],
        gateway: ApprovalCommentGateway,
        *,
        dispatcher_id: str | None = None,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> None:
        self._session_factory = session_factory
        self._gateway = gateway
        self._lease_seconds = lease_seconds
        self._poll_interval = poll_interval
        self.dispatcher_id = dispatcher_id or (
            "outbox-"
            + "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
        )

    # ------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------

    def run_once(self, *, now: datetime | None = None) -> bool:
        """领取并送达一个事件；返回是否做了工作。"""
        moment = now or utcnow()
        recycle_expired_leases(self._open(), now=moment)

        session = self._open()
        try:
            claimed = claim_next_event(
                session,
                dispatcher_id=self.dispatcher_id,
                lease_seconds=self._lease_seconds,
                now=moment,
            )
            if claimed is None:
                return False
            with correlation_scope(claimed.correlation_id):
                self._deliver(session, claimed, now=moment)
            return True
        finally:
            session.close()

    def run_forever(self, *, max_iterations: int | None = None) -> None:
        """常驻轮询；`max_iterations` 用于冒烟与演练。"""
        iterations = 0
        while max_iterations is None or iterations < max_iterations:
            if not self.run_once():
                time.sleep(self._poll_interval)
            iterations += 1

    def _open(self):
        return self._session_factory()

    # ------------------------------------------------------------
    # 送达
    # ------------------------------------------------------------

    def _deliver(self, session, claimed: ClaimedEvent, *, now: datetime) -> None:
        moment = now or utcnow()

        if claimed.event_type != OutboxEventType.WRITE_APPROVAL_COMMENT.value:
            self._reject_unknown(session, claimed)
            return

        attempt = session.get(CommentLog, claimed.aggregate_id)
        if attempt is None:
            # 意图指向的尝试行不存在：确定性故障，不重试
            self._apply_failure(
                session,
                claimed,
                error=PermanentError(
                    f"事件 {claimed.event_id} 指向的尝试 {claimed.aggregate_id} 不存在",
                    code=ErrorCode.RESOURCE_NOT_FOUND,
                ),
                now=moment,
            )
            return

        instance_id = claimed.payload.get("instance_id")
        result_id = claimed.payload.get("result_id")
        expected_digest = claimed.payload.get("content_digest")
        if not isinstance(instance_id, str) or not isinstance(result_id, int):
            self._apply_failure(
                session,
                claimed,
                error=PermanentError(
                    f"事件 {claimed.event_id} 的 payload 缺少定位标识",
                    code=ErrorCode.UNEXPECTED_ERROR,
                ),
                now=moment,
            )
            return

        # 重载结果（payload 只带标识与摘要，正文由派发器按 result_id 重载）
        try:
            view = get_result_view(session, result_id=result_id)
        except ResultInputError as error:
            self._apply_failure(session, claimed, error=_as_permanent(error), now=moment)
            return
        if view.content_digest != expected_digest:
            self._apply_failure(
                session,
                claimed,
                error=PermanentError(
                    f"事件 {claimed.event_id} 的正文摘要与结果 {result_id} 不一致",
                    code=ErrorCode.UNEXPECTED_ERROR,
                ),
                now=moment,
            )
            return

        # ---- 超时对账：先查再决定重发 ----
        if claimed.last_error_code == ErrorCode.APPROVAL_API_TIMEOUT.value:
            found = self._gateway.get_write_result(instance_id, claimed.idempotency_key)
            if found is not None and found.write_status == WriteStatus.SUCCESS.value:
                self._apply_success(session, claimed, found, now=moment)
                return

        # ---- 真正的发送 ----
        try:
            dto = self._gateway.write_comment(
                instance_id,
                view.comment_text,
                idempotency_key=claimed.idempotency_key,
                operator_name=attempt.operator_name,
            )
        except TransientError as error:
            self._apply_failure(session, claimed, error=error, now=moment)
            return
        except PermanentError as error:
            self._apply_failure(session, claimed, error=error, now=moment)
            return

        if dto.write_status == WriteStatus.SUCCESS.value:
            self._apply_success(session, claimed, dto, now=moment)
            return
        self._apply_failure(
            session,
            claimed,
            error=PermanentError(
                f"网关返回写入状态 {dto.write_status}",
                code=ErrorCode.APPROVAL_API_ERROR,
            ),
            now=moment,
        )

    # ------------------------------------------------------------
    # 结果记账（一件事一个事务）
    # ------------------------------------------------------------

    def _apply_success(
        self,
        session,
        claimed: ClaimedEvent,
        dto: WriteCommentResultDTO,
        *,
        now: datetime,
    ) -> None:
        """送达记账：Outbox + 尝试 + 任务 + 审计，**同一事务**。"""
        rowcount = session.execute(
            update(OutboxEvent)
            .where(
                OutboxEvent.id == claimed.event_id,
                OutboxEvent.event_status == OutboxStatus.PENDING.value,
                OutboxEvent.lease_owner == self.dispatcher_id,
            )
            .values(
                event_status=OutboxStatus.DELIVERED.value,
                delivered_at=now,
                lease_owner=None,
                lease_expires_at=None,
                last_error_code=None,
                last_error_text=None,
            )
        ).rowcount
        if rowcount == 0:
            # 租约已失守（长时间外部调用后到期被接管）：外部效果可能
            # 已由接管者达成，本地记账交给它；丢弃本次结果即可。
            session.rollback()
            return

        attempt = session.get(CommentLog, claimed.aggregate_id)
        assert attempt is not None
        attempt.write_status = WriteStatus.SUCCESS.value
        attempt.reason_code = None
        attempt.reason_text = None
        attempt.write_response_text = dto.response_text

        task = session.get(ApprovalTask, attempt.task_id)
        if task is not None:
            task.write_status = WriteStatus.SUCCESS.value
            # 任务收口：reviewing → done（done 是终态，不可从 blocked 进入）
            if can_transition(task.task_status, TaskStatus.DONE):
                transition(task, TaskStatus.DONE, reason="回写送达")
            session.add(
                AuditEvent(
                    task_id=task.id,
                    actor_name="system",
                    action=AuditAction.WRITEBACK_DELIVERED.value,
                    target_type="comment_log",
                    target_id=attempt.id,
                    correlation_id=claimed.correlation_id,
                    detail_json=json.dumps(
                        {
                            "result_id": claimed.payload.get("result_id"),
                            "attempt_id": attempt.id,
                            "external_comment_id": dto.external_comment_id,
                            "replayed": dto.replayed,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                )
            )
            LogService(session).log(
                log_type=LogType.WRITEBACK,
                task_id=task.id,
                level=LogLevel.INFO,
                message=(
                    "回写送达"
                    + ("（幂等重放）" if dto.replayed else "")
                    + f"，外部评论 {dto.external_comment_id}"
                ),
                payload={
                    "outbox_event_id": claimed.event_id,
                    "attempt_id": attempt.id,
                },
            )
        session.commit()

    def _apply_failure(
        self,
        session,
        claimed: ClaimedEvent,
        *,
        error: AppError,
        now: datetime,
    ) -> None:
        """失败记账：还有预算 → 退避重试；耗尽/确定性 → failed + blocked。"""
        exhausted = (not error.retryable) or claimed.attempt_no >= claimed.max_attempts

        if not exhausted:
            rowcount = session.execute(
                update(OutboxEvent)
                .where(
                    OutboxEvent.id == claimed.event_id,
                    OutboxEvent.event_status == OutboxStatus.PENDING.value,
                    OutboxEvent.lease_owner == self.dispatcher_id,
                )
                .values(
                    next_retry_at=now + timedelta(
                        seconds=backoff_seconds(claimed.attempt_no)
                    ),
                    last_error_code=str(error.code),
                    last_error_text=error.message,
                    lease_owner=None,
                    lease_expires_at=None,
                )
            ).rowcount
            if rowcount == 0:
                session.rollback()
                return
            attempt = session.get(CommentLog, claimed.aggregate_id)
            if attempt is not None:
                LogService(session).log(
                    log_type=LogType.WRITEBACK,
                    task_id=attempt.task_id,
                    level=LogLevel.WARNING,
                    message=f"回写暂未送达，将退避重试：{error.message}",
                    error_code=error.code,
                    payload={"outbox_event_id": claimed.event_id},
                )
            session.commit()
            return

        rowcount = session.execute(
            update(OutboxEvent)
            .where(
                OutboxEvent.id == claimed.event_id,
                OutboxEvent.event_status == OutboxStatus.PENDING.value,
                OutboxEvent.lease_owner == self.dispatcher_id,
            )
            .values(
                event_status=OutboxStatus.FAILED.value,
                last_error_code=str(error.code),
                last_error_text=error.message,
                lease_owner=None,
                lease_expires_at=None,
            )
        ).rowcount
        if rowcount == 0:
            session.rollback()
            return

        attempt = session.get(CommentLog, claimed.aggregate_id)
        if attempt is not None:
            attempt.write_status = WriteStatus.FAILED.value
            attempt.reason_code = str(error.code)
            attempt.reason_text = error.message

            task = session.get(ApprovalTask, attempt.task_id)
            if task is not None:
                task.write_status = WriteStatus.FAILED.value
                # 阻塞在回写：恢复点回 reviewing（只重试回写，不重跑解析/规则）
                if can_transition(task.task_status, TaskStatus.BLOCKED):
                    mark_blocked(
                        task,
                        stage=JobType.WRITEBACK,
                        error_code=error.code,
                        message=f"回写失败：{error.message}",
                    )
                LogService(session).log(
                    log_type=LogType.WRITEBACK,
                    task_id=task.id,
                    level=LogLevel.ERROR,
                    message=f"回写失败（不再重试）：{error.message}",
                    error_code=error.code,
                    payload={"outbox_event_id": claimed.event_id},
                )
        session.commit()

    def _reject_unknown(self, session, claimed: ClaimedEvent) -> None:
        """拒绝无法路由的事件：failed + 稳定标识，**绝不**标记送达。"""
        session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == claimed.event_id)
            .values(
                event_status=OutboxStatus.FAILED.value,
                last_error_code=UNKNOWN_EVENT_TYPE,
                last_error_text=(
                    f"未知事件类型 {claimed.event_type!r}"
                    f"（aggregate={claimed.aggregate_type!r}）"
                ),
                lease_owner=None,
                lease_expires_at=None,
            )
        )
        LogService(session).log(
            log_type=LogType.SYSTEM,
            level=LogLevel.ERROR,
            message=f"Outbox 拒绝未知事件类型：{claimed.event_type!r}",
            error_code=ErrorCode.UNEXPECTED_ERROR,
            payload={"outbox_event_id": claimed.event_id},
        )
        session.commit()


def _as_permanent(error: ResultInputError) -> PermanentError:
    """把结果缺失转成确定性网关语义：重试不会让结果回来。"""
    return PermanentError(
        error.message,
        code=ErrorCode.RESOURCE_NOT_FOUND,
    )
