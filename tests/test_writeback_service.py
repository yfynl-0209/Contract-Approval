"""回写策略与事务性意图的测试（M6 / Task 4）。

## 本文件守住的四类"写错了也不报错"

1. **不该回写的被放行** —— 门禁（task/result 匹配、立场上下文可信、
   结果确认、正文、已回写）任何一条不满足都必须拒绝，且拒绝是
   `write_status=not_written` + **稳定原因码**（`WritebackReasonCode`，
   M2 起的领域词汇表），不是 500 或静默放行。
2. **拒绝却留下了"意图"** —— 门禁拒绝时创建 Outbox 事件会让派发器
   把一次被拒绝的回写"补发"出去：拒绝必须**只**留拒绝证据，不留意图。
3. **意图与 Outbox 不在同一事务** —— 业务事务里只写 comment_logs、
   崩溃在 Outbox 之前 → "有 attempt 没 event"，派发器永远看不见，
   任务停在 writing。两者必须同生共死（回滚测试守这条）。
4. **重放被当成新尝试**（或拒绝把后来的合法重试堵死）—— 幂等键 =
   规范化(provider, tenant, instance, result, content_digest) 的 SHA-256：
   完全相同的请求返回**同一**次尝试；曾被拒绝的尝试在条件修复后
   能用**同一行**转正，而不是被旧的拒绝行永久挡住。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.auth import Actor
from app.config import PROJECT_ROOT, Settings
from app.enums import (
    AuditAction,
    ContextStatus,
    OutboxEventType,
    OutboxStatus,
    WriteStatus,
    WritebackReasonCode,
)
from app.models import (
    ApprovalAttachment,
    ApprovalTask,
    AuditEvent,
    CommentLog,
    ContractParse,
    OutboxEvent,
    ReviewResult,
    ReviewRun,
)
from app.services import writeback_service
from app.services.result_service import ResultInputError, ResultView
from app.services.writeback_service import (
    GateDecision,
    WritebackRef,
    evaluate_writeback_gate,
    request_writeback,
    writeback_idempotency_key_of,
)

SCHEMA_FILE = PROJECT_ROOT / "db" / "schema.sql"


def _actor(name: str) -> Actor:
    """测试身份：`actor_id` 与 `display_name` 同名，断言时更好读。

    ⚠️ `roles` 刻意留空 —— 本文件测的是**服务层**，而权限判断是
    API 层 `require_permissions` 的职责（见 tests/test_auth_rbac.py）。
    这里补一套"看起来合理"的角色，会让"服务层是否偷偷鉴权"再也测不出来。
    """
    return Actor(
        actor_id=name, display_name=name, roles=frozenset(), tenant_id="default"
    )


@pytest.fixture()
def session(work_dir: Path) -> Session:
    """独立库：用**交付的 schema.sql** 建表（同 test_result_service 的约定）。"""
    path = work_dir / "writeback.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()

    engine = create_engine(f"sqlite:///{path.as_posix()}", future=True)
    try:
        with sessionmaker(bind=engine, future=True)() as active:
            yield active
    finally:
        engine.dispose()


# ============================================================
# 种子与构造工具
# ============================================================


def _seed(
    session: Session,
    *,
    instance_id: str = "HT-1",
    risk: str = "low",
    review_status: str = "complete",
    comment_text: str = "回写正文",
    context_status: str = ContextStatus.CONFIRMED.value,
    confirmed: bool = False,
    task_write_status: str = WriteStatus.NOT_WRITTEN.value,
) -> tuple[ApprovalTask, ReviewResult]:
    """最小可用的 任务→附件→解析→批次→结果 链（外键全部真实成立）。"""
    task = ApprovalTask(
        provider="mock",
        tenant_id="default",
        instance_id=instance_id,
        approval_code="HT-2026-0001",
        task_status="reviewing",
        write_status=task_write_status,
        context_status=context_status,
        our_party_name="我方公司",
        our_party_contract_label="party_a",
        our_party_business_role="buyer",
        contract_type="procurement",
    )
    session.add(task)
    session.flush()

    attachment = ApprovalAttachment(
        task_id=task.id,
        attachment_id="ATT-1",
        file_name="contract.pdf",
        download_status="success",
        content_type="application/pdf",
    )
    session.add(attachment)
    session.flush()

    parse = ContractParse(
        task_id=task.id,
        attachment_id=attachment.id,
        parse_status="succeeded",
        parse_version=1,
    )
    session.add(parse)
    session.flush()

    run = ReviewRun(
        task_id=task.id,
        parse_id=parse.id,
        version_no=1,
        run_status="completed",
    )
    session.add(run)
    session.flush()

    digest = hashlib.sha256(comment_text.encode("utf-8")).hexdigest()
    result = ReviewResult(
        task_id=task.id,
        run_id=run.id,
        overall_risk_level=risk,
        review_status=review_status,
        hit_count=1,
        needs_review_count=0,
        not_applicable_count=0,
        summary_text="审查摘要",
        focus_points_json="[]",
        comment_text=comment_text,
        content_digest=digest,
        result_fingerprint=f"fp-{risk}-{review_status}-{comment_text}",
        version_no=1,
        created_by="reviewer-1",
    )
    if confirmed:
        result.manual_confirmed = 1
        result.confirmed_by = "reviewer-9"
        result.confirmed_digest = digest
        result.confirmed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    session.add(result)
    session.flush()
    return task, result


def _task(**overrides: object) -> ApprovalTask:
    """门禁单测用的任务对象（不落库；id 显式给定）。"""
    task = ApprovalTask(
        provider="mock",
        tenant_id="default",
        instance_id="HT-1",
        approval_code="HT-2026-0001",
        task_status="reviewing",
        write_status=WriteStatus.NOT_WRITTEN.value,
        context_status=ContextStatus.CONFIRMED.value,
    )
    task.id = overrides.pop("id", 1)  # type: ignore[arg-type]
    for key, value in overrides.items():
        setattr(task, key, value)
    return task


def _view(**overrides: object) -> ResultView:
    """门禁单测用的结果视图（get_result_view 的纯数据形态）。"""
    fields: dict[str, object] = {
        "result_id": 11,
        "run_id": 1,
        "task_id": 1,
        "version_no": 1,
        "is_current_version": True,
        "confirmation_valid": False,
        "overall_risk_level": "low",
        "review_status": "complete",
        "hit_count": 0,
        "needs_review_count": 0,
        "not_applicable_count": 0,
        "summary_text": "摘要",
        "focus_points": [],
        "comment_text": "回写正文",
        "content_digest": "d" * 64,
        "manual_confirmed": False,
        "confirmed_by": None,
        "confirmed_at": None,
        "confirmed_digest": None,
        "supersedes_result_id": None,
        "created_by": None,
        "created_at": None,
        "updated_at": None,
    }
    fields.update(overrides)
    return ResultView(**fields)  # type: ignore[arg-type]


def _settings(*, auto: bool = False) -> Settings:
    return Settings(auto_writeback_enabled=auto)


def _count(session: Session, model: type) -> int:
    return session.execute(select(func.count()).select_from(model)).scalar_one()


# ============================================================
# 1. 门禁（表驱动：每条拒绝路径一个稳定原因码）
# ============================================================


@pytest.mark.parametrize(
    ("task_kwargs", "view_kwargs", "auto", "expected"),
    [
        # ---- task/result 不匹配：结果不属于这个任务 ----
        pytest.param(
            {"id": 1}, {"task_id": 2}, False, WritebackReasonCode.RESULT_MISSING,
            id="task-result-mismatch",
        ),
        # ---- 立场上下文不可信（缺失）----
        pytest.param(
            {"context_status": "missing"}, {}, False,
            WritebackReasonCode.CONTEXT_NOT_VALID, id="context-missing",
        ),
        # ---- 立场上下文不可信（冲突）----
        pytest.param(
            {"context_status": "conflict"}, {}, False,
            WritebackReasonCode.CONTEXT_NOT_VALID, id="context-conflict",
        ),
        # ---- 没有可回写的正文 ----
        pytest.param(
            {}, {"comment_text": "   "}, False, WritebackReasonCode.COMMENT_TEXT_MISSING,
            id="comment-missing",
        ),
        # ---- 已经成功回写过：先答"已回写"，再谈确认 ----
        pytest.param(
            {"write_status": "success"}, {}, False,
            WritebackReasonCode.ALREADY_WRITTEN, id="already-delivered",
        ),
        # ---- 高风险：即使打开自动回写也必须先人工确认 ----
        pytest.param(
            {}, {"overall_risk_level": "high"}, True,
            WritebackReasonCode.MANUAL_CONFIRM_REQUIRED, id="high-risk-unconfirmed",
        ),
        # ---- 待人工判断：同上，不可被自动回写绕过 ----
        pytest.param(
            {}, {"review_status": "needs_review"}, True,
            WritebackReasonCode.MANUAL_CONFIRM_REQUIRED,
            id="needs-review-unconfirmed",
        ),
        # ---- 确认已失效（确认过，但版本被接替 / 正文已变）----
        pytest.param(
            {}, {"manual_confirmed": True, "confirmation_valid": False}, True,
            WritebackReasonCode.MANUAL_CONFIRM_REQUIRED, id="stale-confirmation",
        ),
        # ---- 低/中风险完整结果未确认，自动回写关闭（默认）----
        pytest.param(
            {}, {}, False, WritebackReasonCode.MANUAL_CONFIRM_REQUIRED,
            id="low-unconfirmed-auto-disabled",
        ),
    ],
)
def test_gate_denies_each_case_with_a_stable_reason_code(
    task_kwargs: dict, view_kwargs: dict, auto: bool, expected: WritebackReasonCode
) -> None:
    """**Task 4 核心断言**：每条拒绝路径一个稳定原因码，绝不静默放行。"""
    decision = evaluate_writeback_gate(
        _task(**task_kwargs), _view(**view_kwargs), _settings(auto=auto)
    )
    assert isinstance(decision, GateDecision)
    assert decision.allowed is False
    assert decision.reason_code == expected.value


@pytest.mark.parametrize(
    ("task_kwargs", "view_kwargs", "auto"),
    [
        # 高风险 / 待判断，但确认有效 → 放行
        pytest.param(
            {}, {"overall_risk_level": "high", "confirmation_valid": True}, False,
            id="high-confirmed",
        ),
        pytest.param(
            {}, {"review_status": "needs_review", "confirmation_valid": True}, False,
            id="needs-review-confirmed",
        ),
        # 低/中风险完整结果未确认，但打开了自动回写 → 放行（Fixed Decision 4）
        pytest.param(
            {}, {}, True, id="low-unconfirmed-auto-enabled",
        ),
        # 普通确认路径
        pytest.param(
            {}, {"confirmation_valid": True}, False, id="low-confirmed",
        ),
        # 立场上下文四项齐全（complete，未另行人工确认）同样可信 → 放行
        pytest.param(
            {"context_status": "complete"}, {"confirmation_valid": True}, False,
            id="context-complete-confirmed-result",
        ),
    ],
)
def test_gate_allows_confirmed_or_automated_results(
    task_kwargs: dict, view_kwargs: dict, auto: bool
) -> None:
    decision = evaluate_writeback_gate(
        _task(**task_kwargs), _view(**view_kwargs), _settings(auto=auto)
    )
    assert decision.allowed is True
    assert decision.reason_code is None


# ============================================================
# 2. 拒绝：not_written + 稳定原因码，且**不创建** Outbox 事件
# ============================================================


def test_denial_is_recorded_as_not_written_without_an_outbox_event(
    session: Session,
) -> None:
    task, result = _seed(session, context_status="missing", confirmed=True)

    ref = request_writeback(
        session, instance_id="HT-1", result_id=result.id, actor=_actor("operator-1")
    )

    assert isinstance(ref, WritebackRef)
    assert ref.write_status == WriteStatus.NOT_WRITTEN.value
    assert ref.reason_code == WritebackReasonCode.CONTEXT_NOT_VALID.value
    assert ref.reused is False

    row = session.get(CommentLog, ref.attempt_id)
    assert row is not None
    assert row.write_status == WriteStatus.NOT_WRITTEN.value
    assert row.reason_code == WritebackReasonCode.CONTEXT_NOT_VALID.value
    assert row.operator_name == "operator-1"

    # 拒绝绝不产生回写意图：Outbox 里没有事件，审计里没有"已发起回写"
    assert _count(session, OutboxEvent) == 0
    requested = [
        e
        for e in session.execute(select(AuditEvent)).scalars()
        if e.action == AuditAction.WRITEBACK_REQUESTED.value
    ]
    assert requested == []
    # 任务级状态不被拒绝污染
    assert task.write_status == WriteStatus.NOT_WRITTEN.value


def test_instance_mismatch_is_a_recorded_denial(session: Session) -> None:
    """结果属于 HT-1，调用方却说 HT-OTHER —— 拒绝必须留痕，而不是 500。"""
    _, result = _seed(session, confirmed=True)

    ref = request_writeback(
        session, instance_id="HT-OTHER", result_id=result.id, actor=_actor("operator-1")
    )
    assert ref.write_status == WriteStatus.NOT_WRITTEN.value
    assert ref.reason_code == WritebackReasonCode.RESULT_MISSING.value


def test_missing_result_raises_a_stable_error(session: Session) -> None:
    with pytest.raises(ResultInputError) as info:
        request_writeback(session, instance_id="HT-1", result_id=999, actor=_actor("operator-1"))
    assert info.value.reason_code == "RESULT_NOT_FOUND"


# ============================================================
# 3. 放行：一个事务里 意图 + Outbox + 审计
# ============================================================


def test_allowed_request_creates_intent_outbox_and_audit_together(
    session: Session,
) -> None:
    task, result = _seed(session, confirmed=True)

    ref = request_writeback(
        session, instance_id="HT-1", result_id=result.id, actor=_actor("operator-1")
    )

    assert ref.write_status == WriteStatus.WRITING.value
    assert ref.reason_code is None
    assert ref.reused is False
    assert ref.outbox_event_id is not None

    # ---- 意图（comment_logs）----
    attempt = session.get(CommentLog, ref.attempt_id)
    assert attempt is not None
    assert attempt.write_status == WriteStatus.WRITING.value
    assert attempt.reason_code is None
    assert attempt.content_digest == result.content_digest
    expected_key = writeback_idempotency_key_of(task, _view_for(result))
    assert attempt.idempotency_key == expected_key

    # ---- Outbox：pending + 已知事件类型 + 只带标识与摘要 ----
    event = session.get(OutboxEvent, ref.outbox_event_id)
    assert event is not None
    assert event.event_status == OutboxStatus.PENDING.value
    assert event.event_type == OutboxEventType.WRITE_APPROVAL_COMMENT.value
    assert event.aggregate_type == "comment_log"
    assert event.aggregate_id == attempt.id
    payload = json.loads(event.payload_json)
    assert payload["instance_id"] == "HT-1"
    assert payload["result_id"] == result.id
    assert payload["content_digest"] == result.content_digest
    # 决策 ⑤：事件只带标识与摘要，不带正文 / 指针类字段
    assert "comment_text" not in payload
    assert "file_path" not in payload
    assert "object_key" not in payload

    # ---- 审计：WRITEBACK_REQUESTED，目标指向这次尝试 ----
    events = [
        e
        for e in session.execute(select(AuditEvent)).scalars()
        if e.action == AuditAction.WRITEBACK_REQUESTED.value
    ]
    assert len(events) == 1
    assert events[0].actor_name == "operator-1"
    assert events[0].target_type == "comment_log"
    assert events[0].target_id == attempt.id

    # ---- 任务级状态同步进入 writing ----
    assert task.write_status == WriteStatus.WRITING.value


def _view_for(result: ReviewResult) -> ResultView:
    """把 ORM 结果行转成视图（键计算只依赖标识与摘要字段）。"""
    return ResultView(
        result_id=result.id,
        run_id=result.run_id,
        task_id=result.task_id,
        version_no=result.version_no,
        is_current_version=True,
        confirmation_valid=True,
        overall_risk_level=result.overall_risk_level,
        review_status=result.review_status,
        hit_count=result.hit_count,
        needs_review_count=result.needs_review_count,
        not_applicable_count=result.not_applicable_count,
        summary_text=result.summary_text or "",
        focus_points=[],
        comment_text=result.comment_text or "",
        content_digest=result.content_digest or "",
        manual_confirmed=bool(result.manual_confirmed),
        confirmed_by=result.confirmed_by,
        confirmed_at=result.confirmed_at,
        confirmed_digest=result.confirmed_digest,
        supersedes_result_id=result.supersedes_result_id,
        created_by=result.created_by,
        created_at=result.created_at,
        updated_at=result.updated_at,
    )


# ============================================================
# 4. 幂等：相同请求返回同一次尝试；拒绝不堵死后续合法重试
# ============================================================


def test_identical_replay_returns_the_same_attempt(session: Session) -> None:
    _, result = _seed(session, confirmed=True)

    first = request_writeback(
        session, instance_id="HT-1", result_id=result.id, actor=_actor("operator-1")
    )
    replay = request_writeback(
        session, instance_id="HT-1", result_id=result.id, actor=_actor("operator-2")
    )

    assert replay.reused is True
    assert replay.attempt_id == first.attempt_id
    assert replay.write_status == WriteStatus.WRITING.value
    assert _count(session, CommentLog) == 1
    assert _count(session, OutboxEvent) == 1


def test_denied_attempt_transitions_the_same_row_once_conditions_are_fixed(
    session: Session,
) -> None:
    """曾被拒绝的尝试在条件修复后**复用同一行**转正。

    拒绝行占着幂等键（唯一约束），如果重试另建新行必然撞键；
    而如果重试被旧行挡住，"先拒绝后修复"的任务就永远写不出去。
    """
    task, result = _seed(session, context_status="missing", confirmed=True)

    denied = request_writeback(
        session, instance_id="HT-1", result_id=result.id, actor=_actor("operator-1")
    )
    assert denied.write_status == WriteStatus.NOT_WRITTEN.value

    # 修复条件：人工确认立场
    task.context_status = ContextStatus.CONFIRMED.value
    session.flush()

    allowed = request_writeback(
        session, instance_id="HT-1", result_id=result.id, actor=_actor("operator-1")
    )
    assert allowed.write_status == WriteStatus.WRITING.value
    assert allowed.attempt_id == denied.attempt_id  # 同一行转正，不另建
    assert _count(session, CommentLog) == 1
    assert _count(session, OutboxEvent) == 1


def test_idempotency_key_binds_identity_and_digest() -> None:
    """键 = 规范化(provider, tenant, instance, result, digest) 的 SHA-256。"""
    task = _task()
    base = _view()
    assert writeback_idempotency_key_of(task, base) == writeback_idempotency_key_of(
        _task(), _view()
    )
    # 正文变了（digest 变）→ 新键
    assert writeback_idempotency_key_of(task, base) != writeback_idempotency_key_of(
        task, _view(content_digest="e" * 64)
    )
    # 结果变了 → 新键
    assert writeback_idempotency_key_of(task, base) != writeback_idempotency_key_of(
        task, _view(result_id=12)
    )
    # 任务变了（instance 变）→ 新键
    assert writeback_idempotency_key_of(task, base) != writeback_idempotency_key_of(
        _task(instance_id="HT-2"), base
    )


# ============================================================
# 5. 事务性：注入失败 → 意图与 Outbox 同生共死
# ============================================================


def test_injected_failure_rolls_back_intent_and_outbox_together(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """comment_logs 插入之后、Outbox 写入之前注入失败：
    回滚后**两者都不在**，任务状态不变 —— 否则会出现"有 attempt 没 event"
    的孤儿意图，任务永远停在 writing。
    """
    task, result = _seed(session, confirmed=True)

    def _boom(*args: object, **kwargs: object) -> OutboxEvent:
        raise RuntimeError("injected failure after comment_logs insertion")

    monkeypatch.setattr(writeback_service, "_create_outbox_event", _boom)

    with pytest.raises(RuntimeError, match="injected failure"):
        request_writeback(session, instance_id="HT-1", result_id=result.id, actor=_actor("operator-1"))

    session.rollback()

    assert _count(session, CommentLog) == 0
    assert _count(session, OutboxEvent) == 0
    requested = [
        e
        for e in session.execute(select(AuditEvent)).scalars()
        if e.action == AuditAction.WRITEBACK_REQUESTED.value
    ]
    assert requested == []
    assert task.write_status == WriteStatus.NOT_WRITTEN.value
