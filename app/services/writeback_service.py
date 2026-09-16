"""回写策略与事务性意图（M6 / Task 4）。

## 为什么回写需要"策略 + 意图"两层

外部调用不可与本地事务原子提交。直接调用审批系统会留下两个窗口：
先调外部、崩溃在落库前 → 回写丢失且无人知晓；先落库"已成功"、
崩溃在调用前 → 谎报成功。本模块的做法：

1. **策略层**（`evaluate_writeback_gate`）：纯函数门禁，回答"这次回写
   **允许不允许发起**"。不允许 → `write_status=not_written` +
   `WritebackReasonCode` 稳定原因码，**不创建任何 Outbox 事件**。
2. **意图层**（`request_writeback`）：允许时，在**同一个事务**里写
   `comment_logs(writing)` + `outbox_events(pending)` + 审计事件 ——
   意图与业务状态同生共死，送达由 Task 5 的派发器独立保证。

## 幂等

幂等键 = 规范化 `(provider, tenant, instance, result, content_digest)`
的 SHA-256：完全相同的请求返回**同一次**尝试。曾被门禁拒绝的尝试行
（`not_written`）在条件修复后**复用同一行**转正 —— 拒绝行占着唯一键，
重试若另建新行必然撞键；若被旧行挡住，"先拒绝后修复"的任务就永远
写不出去。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import Actor
from app.config import settings as _default_settings
from app.context import get_correlation_id
from app.enums import (
    AuditAction,
    ContextStatus,
    LogType,
    LogLevel,
    OutboxEventType,
    ReviewStatus,
    RiskLevel,
    WriteStatus,
    WritebackReasonCode,
)
from app.models import ApprovalTask, AuditEvent, CommentLog, OutboxEvent
from app.services.log_service import LogService
from app.services.result_service import (
    ResultInputError,
    ResultView,
    get_result_view,
)


class _GateSettings(Protocol):
    """门禁只关心这一个开关（避免把整个 Settings 焊死在签名里）。"""

    auto_writeback_enabled: bool


@dataclass(frozen=True)
class GateDecision:
    """门禁裁决：允许与否 + 拒绝时的稳定原因码。"""

    allowed: bool
    reason_code: str | None = None
    reason_text: str = ""


@dataclass(frozen=True)
class WritebackRef:
    """`request_writeback` 的返回：指向 comment_logs 里那次尝试。"""

    attempt_id: int | None
    task_id: int
    instance_id: str
    result_id: int
    write_status: str
    #: 稳定机器判据（`WritebackReasonCode`）——调用方凭它分支
    reason_code: str | None = None
    #: 人读原因。⚠️ 与 `reason_code` 分工：程序**永远**不要解析这段文本，
    #: 它的措辞会随文案调整而变（与 `comment_logs.reason_code/reason_text`
    #: 的同一分工，见 `app/models.py`）。
    #: 门禁拒绝（`not_written`）时在接口上必须给得出来 —— 拒绝若是无法解释的，
    #: 调用方唯一的处置就是反复重试同一个注定被拒的请求。
    reason_text: str | None = None
    #: 幂等重放（或拒绝行复用）时为 True —— 调用方能区分"新尝试"与"旧尝试"
    reused: bool = False
    outbox_event_id: int | None = None


#: 立场上下文"可信"的取值：四项齐全（complete）或已人工确认（confirmed）。
#: 缺失 / 冲突都不可信 —— `WritebackReasonCode.CONTEXT_NOT_VALID` 的口径。
_TRUSTED_CONTEXT: tuple[str, str] = (
    ContextStatus.COMPLETE.value,
    ContextStatus.CONFIRMED.value,
)


# ============================================================
# 策略层：门禁（纯函数，不碰数据库）
# ============================================================


def evaluate_writeback_gate(
    task: ApprovalTask, result: ResultView, settings: _GateSettings
) -> GateDecision:
    """回写门禁。

    Args:
        task:      候选任务（含 `context_status` / `write_status`）。
        result:    候选结果视图（含确认有效性与正文摘要）。
        settings:  只读 `auto_writeback_enabled`。

    Returns:
        `GateDecision`：`allowed=False` 时 `reason_code` 必为
        `WritebackReasonCode` 的稳定取值 —— 拒绝是**可解释的**，
        不是 500，也不是静默放行。
    """
    if result.task_id != task.id:
        return GateDecision(
            False,
            WritebackReasonCode.RESULT_MISSING.value,
            f"结果 {result.result_id} 不属于任务 {task.id}",
        )

    if task.context_status not in _TRUSTED_CONTEXT:
        return GateDecision(
            False,
            WritebackReasonCode.CONTEXT_NOT_VALID.value,
            f"立场上下文不可信（context_status={task.context_status}），"
            "缺失或冲突的结果不能回写",
        )

    if not (result.comment_text or "").strip():
        return GateDecision(
            False,
            WritebackReasonCode.COMMENT_TEXT_MISSING.value,
            "结果没有可回写的正文",
        )

    # "已成功回写"是更根本的事实：先答它，再谈确认（排障第一问是
    # "到底写没写出去"，而不是"确认状态是什么"）。
    if task.write_status == WriteStatus.SUCCESS.value:
        return GateDecision(
            False,
            WritebackReasonCode.ALREADY_WRITTEN.value,
            "该任务已成功回写过",
        )

    # ---- 结果确认（Fixed Decision 4）----
    # 高风险 / 待人工判断**永远**要求有效确认；低/中风险的完整结果
    # 只有在 auto_writeback_enabled 时才可绕过；确认已失效（版本被
    # 接替 / 正文已变）同样必须重新确认。
    if not result.confirmation_valid:
        if result.manual_confirmed:
            return GateDecision(
                False,
                WritebackReasonCode.MANUAL_CONFIRM_REQUIRED.value,
                "结果确认已失效（新版本或正文变更），需对当前版本重新确认",
            )
        if result.overall_risk_level == RiskLevel.HIGH.value:
            return GateDecision(
                False,
                WritebackReasonCode.MANUAL_CONFIRM_REQUIRED.value,
                "高风险结果必须人工确认，自动回写不可绕过",
            )
        if result.review_status == ReviewStatus.NEEDS_REVIEW.value:
            return GateDecision(
                False,
                WritebackReasonCode.MANUAL_CONFIRM_REQUIRED.value,
                "待人工判断的结果必须先确认才能回写",
            )
        if not settings.auto_writeback_enabled:
            return GateDecision(
                False,
                WritebackReasonCode.MANUAL_CONFIRM_REQUIRED.value,
                "结果尚未人工确认，且自动回写未开启",
            )

    return GateDecision(True)


# ============================================================
# 幂等键
# ============================================================


def writeback_idempotency_key_of(task: ApprovalTask, result: ResultView) -> str:
    """幂等键 = 规范化(provider, tenant, instance, result, digest) 的 SHA-256。

    - **不含** approval_code：同一任务重新拉取后 code 可能变化，
      而 (provider, tenant, instance) 才是任务的稳定身份；
    - **含** content_digest：正文变了就是**另一次**回写（版本接替），
      不许复用旧尝试。
    """
    canonical = json.dumps(
        {
            "provider": task.provider,
            "tenant_id": task.tenant_id,
            "instance_id": task.instance_id,
            "result_id": result.result_id,
            "content_digest": result.content_digest,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ============================================================
# 意图层：发起回写（事务性意图）
# ============================================================


def request_writeback(
    session: Session,
    *,
    instance_id: str,
    result_id: int,
    actor: Actor,
    settings: _GateSettings | None = None,
) -> WritebackRef:
    """登记一次回写意图（工具 7 / `write_approval_comment` 的服务侧入口）。

    门禁拒绝 → `comment_logs(not_written + 稳定原因码)`，**没有** Outbox
    事件、**没有** WRITEBACK_REQUESTED 审计 —— 拒绝只留拒绝证据。
    门禁放行 → 同一事务写 `comment_logs(writing)` + `outbox_events(pending)`
    + 审计事件，并把任务级 `write_status` 推进到 `writing`。

    Raises:
        ResultInputError: `RESULT_NOT_FOUND` —— 结果（或其所属任务）不存在。
    """
    if settings is None:
        settings = _default_settings

    view = get_result_view(session, result_id=result_id)
    task = session.get(ApprovalTask, view.task_id)
    if task is None:
        raise ResultInputError(
            ResultInputError.RESULT_NOT_FOUND,
            f"结果 {result_id} 所属的任务不存在",
        )

    key = writeback_idempotency_key_of(task, view)
    existing = session.execute(
        select(CommentLog).where(CommentLog.idempotency_key == key)
    ).scalar_one_or_none()

    # 幂等重放：已发起 / 已成功 / 已失败的尝试，直接返回**同一次**
    if (
        existing is not None
        and existing.write_status != WriteStatus.NOT_WRITTEN.value
    ):
        return _ref_from_row(task, view, existing, reused=True)

    # ---- 门禁评估（instance 一致性 + 策略门禁）----
    denial: tuple[WritebackReasonCode, str] | None = None
    if task.instance_id != instance_id:
        denial = (
            WritebackReasonCode.RESULT_MISSING,
            f"结果 {view.result_id} 属于任务 {task.instance_id}，"
            f"与请求的 {instance_id} 不一致",
        )
    else:
        decision = evaluate_writeback_gate(task, view, settings)
        if not decision.allowed:
            assert decision.reason_code is not None  # 拒绝必有稳定原因码
            denial = (WritebackReasonCode(decision.reason_code), decision.reason_text)

    if denial is not None:
        return _record_denial(session, task=task, view=view, key=key, existing=existing,
                              code=denial[0], text=denial[1], actor=actor)

    # ---- 放行：一个事务里 意图 + Outbox + 审计 ----
    if existing is not None:
        # 曾被拒绝的行转正：同一行从 not_written → writing
        attempt = existing
        attempt.write_status = WriteStatus.WRITING.value
        attempt.reason_code = None
        attempt.reason_text = None
        attempt.operator_name = actor.display_name
        session.flush()
    else:
        attempt = CommentLog(
            task_id=task.id,
            review_id=view.result_id,
            write_status=WriteStatus.WRITING.value,
            content_digest=view.content_digest,
            idempotency_key=key,
            operator_name=actor.display_name,
        )
        session.add(attempt)
        session.flush()

    event = _create_outbox_event(
        session, attempt=attempt, task=task, result=view, idempotency_key=key
    )

    session.add(
        AuditEvent(
            task_id=task.id,
            # 与结果确认同一约定：**id 与名字都写**（理由见 result_service.confirm_result）
            actor_id=actor.actor_id,
            actor_name=actor.display_name,
            action=AuditAction.WRITEBACK_REQUESTED.value,
            target_type="comment_log",
            target_id=attempt.id,
            correlation_id=get_correlation_id(),
            detail_json=json.dumps(
                {
                    "result_id": view.result_id,
                    "attempt_id": attempt.id,
                    "instance_id": task.instance_id,
                    "content_digest": view.content_digest,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
    )
    task.write_status = WriteStatus.WRITING.value
    LogService(session).log(
        log_type=LogType.WRITEBACK,
        task_id=task.id,
        level=LogLevel.INFO,
        message="回写意图已登记，等待派发",
        payload={
            "result_id": view.result_id,
            "attempt_id": attempt.id,
            "outbox_event_id": event.id,
        },
    )
    session.flush()

    return WritebackRef(
        attempt_id=attempt.id,
        task_id=task.id,
        instance_id=task.instance_id,
        result_id=view.result_id,
        write_status=WriteStatus.WRITING.value,
        reason_code=None,
        reused=existing is not None,
        outbox_event_id=event.id,
    )


def _record_denial(
    session: Session,
    *,
    task: ApprovalTask,
    view: ResultView,
    key: str,
    existing: CommentLog | None,
    code: WritebackReasonCode,
    text: str,
    actor: Actor,
) -> WritebackRef:
    """把一次门禁拒绝落成 `comment_logs(not_written)` 证据行。

    拒绝行也占幂等键：条件修复后的重试会**复用同一行**转正（见
    `request_writeback`），因此拒绝永远不会把合法重试堵死。
    """
    if existing is not None:
        attempt_id = existing.id
        existing.reason_code = code.value
        existing.reason_text = text
        existing.operator_name = actor.display_name
        session.flush()
    else:
        row = CommentLog(
            task_id=task.id,
            review_id=view.result_id,
            write_status=WriteStatus.NOT_WRITTEN.value,
            reason_code=code.value,
            reason_text=text,
            content_digest=view.content_digest,
            idempotency_key=key,
            operator_name=actor.display_name,
        )
        session.add(row)
        session.flush()
        attempt_id = row.id

    LogService(session).log(
        log_type=LogType.WRITEBACK,
        task_id=task.id,
        level=LogLevel.WARNING,
        message=f"回写被门禁拒绝：{text}",
        payload={
            "reason_code": code.value,
            "result_id": view.result_id,
            "attempt_id": attempt_id,
        },
    )
    return WritebackRef(
        attempt_id=attempt_id,
        task_id=task.id,
        instance_id=task.instance_id,
        result_id=view.result_id,
        write_status=WriteStatus.NOT_WRITTEN.value,
        reason_code=code.value,
        reason_text=text,
        reused=existing is not None,
        outbox_event_id=None,
    )


def _create_outbox_event(
    session: Session,
    *,
    attempt: CommentLog,
    task: ApprovalTask,
    result: ResultView,
    idempotency_key: str,
) -> OutboxEvent:
    """创建 Outbox 事件（决策 ⑤：只带标识与摘要，不带正文 / 指针字段）。

    派发器凭 `result_id` 重载结果并核对 `content_digest` 后再发送 ——
    事件里放正文会随版本漂移，放指针（file_path / object_key）则越权暴露。
    """
    payload = json.dumps(
        {
            "provider": task.provider,
            "tenant_id": task.tenant_id,
            "instance_id": task.instance_id,
            "result_id": result.result_id,
            "content_digest": result.content_digest,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    event = OutboxEvent(
        aggregate_type="comment_log",
        aggregate_id=attempt.id,
        event_type=OutboxEventType.WRITE_APPROVAL_COMMENT.value,
        payload_json=payload,
        idempotency_key=idempotency_key,
        correlation_id=get_correlation_id(),
    )
    session.add(event)
    session.flush()
    return event


def _ref_from_row(
    task: ApprovalTask, view: ResultView, row: CommentLog, *, reused: bool
) -> WritebackRef:
    return WritebackRef(
        attempt_id=row.id,
        task_id=task.id,
        instance_id=task.instance_id,
        result_id=view.result_id,
        write_status=row.write_status,
        reason_code=row.reason_code,
        # 已失败 / 已拒绝的尝试重放时，原因文本一并读回 —— 只给机器码
        # 会让"为什么失败"在接口上无解，而库里明明记着。
        reason_text=row.reason_text,
        reused=reused,
        outbox_event_id=None,
    )
