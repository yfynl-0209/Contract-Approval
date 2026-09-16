"""Outbox 派发器与超时对账的测试（M6 / Task 5）。

## 本文件守住的五类"写错了也不报错"

1. **送达与记账脱节** —— Outbox 标 delivered 但 comment_logs 还在
   writing、任务没到 done：三张表必须**同一事务**推进。
2. **未知事件被"跳过"** —— 未知 event_type 标成 delivered 会让事件
   静默丢失且统计上看不出来；必须**拒绝**（failed + 原因），而不是
   假装送达。
3. **超时被当成"没写成功"** —— 超时不代表对方没收到；重试必须**先查
   `get_write_result` 再决定重发**，否则会产生重复评论。
4. **进程死亡窗口** —— 领了租约、外部已写入、本地没记账就死掉：
   租约到期回收后新派发器接管，凭幂等键去重，外部评论**恰好一条**。
5. **重试耗尽自愈成"成功"或无限重试** —— 耗尽后事件/尝试必须
   failed、任务 blocked 在 writeback（恢复点回 reviewing，不重跑
   解析与规则）；瞬时失败则退避重试，不提前判死。
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.auth import Actor
from app.config import PROJECT_ROOT
from app.enums import (
    AuditAction,
    ErrorCode,
    OutboxStatus,
    WriteStatus,
)
from app.errors import PermanentGatewayError, TransientGatewayError
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
from app.outbox import (
    DEFAULT_LEASE_SECONDS,
    OutboxDispatcher,
    claim_next_event,
    recycle_expired_leases,
)
from app.ports.approval_gateway import WriteCommentResultDTO
from app.services.writeback_service import request_writeback
from app.workflow.jobs import backoff_seconds
from app.workflow.state_machine import utcnow

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
def factory(work_dir: Path):
    """独立库：用交付的 schema.sql 建表，返回 session 工厂（同 Worker 测试约定）。"""
    path = work_dir / "outbox.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()

    engine = create_engine(f"sqlite:///{path.as_posix()}", future=True)
    try:
        yield sessionmaker(bind=engine, future=True)
    finally:
        engine.dispose()


# ============================================================
# 种子与测试替身
# ============================================================


def _seed_result(
    session: Session, *, instance_id: str = "HT-1"
) -> tuple[ApprovalTask, ReviewResult]:
    """最小可用的 任务→附件→解析→批次→已确认结果 链。"""
    task = ApprovalTask(
        provider="mock",
        tenant_id="default",
        instance_id=instance_id,
        approval_code="HT-2026-0001",
        task_status="reviewing",
        write_status=WriteStatus.NOT_WRITTEN.value,
        context_status="confirmed",
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

    comment_text = "回写正文"
    digest = hashlib.sha256(comment_text.encode("utf-8")).hexdigest()
    result = ReviewResult(
        task_id=task.id,
        run_id=run.id,
        overall_risk_level="low",
        review_status="complete",
        hit_count=1,
        summary_text="审查摘要",
        focus_points_json="[]",
        comment_text=comment_text,
        content_digest=digest,
        result_fingerprint="fp-outbox-1",
        version_no=1,
        created_by="reviewer-1",
        manual_confirmed=1,
        confirmed_by="reviewer-9",
        confirmed_digest=digest,
    )
    session.add(result)
    session.flush()
    return task, result


def _seed_intent(
    factory, *, instance_id: str = "HT-1"
) -> tuple[int, int, int, int]:
    """建已确认结果并登记回写意图；返回 (task_id, result_id, attempt_id, event_id)。"""
    with factory() as session:
        task, result = _seed_result(session, instance_id=instance_id)
        ref = request_writeback(
            session, instance_id=instance_id, result_id=result.id, actor=_actor("operator-1")
        )
        session.commit()
        event_id = ref.outbox_event_id
        assert event_id is not None
        return task.id, result.id, ref.attempt_id, event_id


class _Record:
    """外部评论记录（测试替身的存储形态）。"""

    def __init__(self, instance_id: str, content: str, key: str, comment_id: str):
        self.instance_id = instance_id
        self.content = content
        self.key = key
        self.comment_id = comment_id


class _FakeGateway:
    """写侧网关测试替身：按幂等键去重（同 mock 网关语义），可注入失败。"""

    provider = "mock"
    tenant_id = "default"

    def __init__(self) -> None:
        self.comments: list[_Record] = []
        self.write_calls = 0
        self.get_calls = 0
        #: (剩余失败次数, 错误码) —— 瞬时失败
        self.fail_times: int = 0
        self.fail_code: ErrorCode = ErrorCode.APPROVAL_API_ERROR
        #: 每次都失败（瞬时）—— 用于重试耗尽
        self.always_fail: bool = False
        #: 每次都失败（确定性）—— 用于"不重试直接判死"
        self.always_fail_permanent: bool = False

    def write_comment(
        self,
        instance_id: str,
        content: str,
        *,
        idempotency_key: str,
        operator_name: str | None = None,
    ) -> WriteCommentResultDTO:
        self.write_calls += 1
        if self.always_fail_permanent:
            raise PermanentGatewayError(
                "实例不存在", code=ErrorCode.INSTANCE_NOT_FOUND
            )
        if self.always_fail:
            raise TransientGatewayError("模拟 5xx", code=self.fail_code)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise TransientGatewayError("模拟超时", code=self.fail_code)

        for record in self.comments:
            if record.instance_id == instance_id and record.key == idempotency_key:
                # 幂等重放：返回第一次的结果，不新增评论
                return self._dto(record, replayed=True)
        record = _Record(
            instance_id, content, idempotency_key, f"cmt-{len(self.comments) + 1}"
        )
        self.comments.append(record)
        return self._dto(record, replayed=False)

    def get_write_result(
        self, instance_id: str, idempotency_key: str
    ) -> WriteCommentResultDTO | None:
        self.get_calls += 1
        for record in self.comments:
            if record.instance_id == instance_id and record.key == idempotency_key:
                return self._dto(record, replayed=True)
        return None

    @staticmethod
    def _dto(record: _Record, *, replayed: bool) -> WriteCommentResultDTO:
        return WriteCommentResultDTO(
            write_status=WriteStatus.SUCCESS,
            external_comment_id=record.comment_id,
            replayed=replayed,
            response_text=f'{{"comment_id": "{record.comment_id}"}}',
        )


def _event(factory, event_id: int) -> OutboxEvent:
    with factory() as session:
        return session.get(OutboxEvent, event_id)


def _attempt(factory, attempt_id: int) -> CommentLog:
    with factory() as session:
        return session.get(CommentLog, attempt_id)


def _task(factory, task_id: int) -> ApprovalTask:
    with factory() as session:
        return session.get(ApprovalTask, task_id)


# ============================================================
# 1. 送达：Outbox / 尝试 / 任务 同一事务推进
# ============================================================


def test_delivery_marks_outbox_attempt_and_task_together(factory) -> None:
    task_id, result_id, attempt_id, event_id = _seed_intent(factory)
    gateway = _FakeGateway()
    dispatcher = OutboxDispatcher(factory, gateway)

    assert dispatcher.run_once() is True

    event = _event(factory, event_id)
    assert event.event_status == OutboxStatus.DELIVERED.value
    assert event.delivered_at is not None

    attempt = _attempt(factory, attempt_id)
    assert attempt.write_status == WriteStatus.SUCCESS.value
    assert attempt.reason_code is None
    assert attempt.write_response_text is not None
    assert "cmt-1" in (attempt.write_response_text or "")

    task = _task(factory, task_id)
    assert task.write_status == WriteStatus.SUCCESS.value
    assert task.task_status == "done"

    delivered = [
        e
        for e in _all(factory, AuditEvent)
        if e.action == AuditAction.WRITEBACK_DELIVERED.value
    ]
    assert len(delivered) == 1

    assert len(gateway.comments) == 1
    # 没有更多待发事件
    assert dispatcher.run_once() is False


def _all(factory, model):
    with factory() as session:
        return list(session.execute(select(model)).scalars())


# ============================================================
# 2. 未知事件类型：拒绝，而不是标送达
# ============================================================


def test_unknown_event_type_is_rejected_not_delivered(factory) -> None:
    with factory() as session:
        session.add(
            OutboxEvent(
                aggregate_type="comment_log",
                aggregate_id=1,
                event_type="SOME_FUTURE_TYPE",
                payload_json='{"instance_id": "HT-1"}',
                idempotency_key="k-unknown-1",
            )
        )
        session.commit()

    gateway = _FakeGateway()
    dispatcher = OutboxDispatcher(factory, gateway)

    assert dispatcher.run_once() is True  # 处理了（拒绝也是处理）

    with factory() as session:
        event = session.execute(select(OutboxEvent)).scalar_one()
        assert event.event_status == OutboxStatus.FAILED.value
        assert event.delivered_at is None  # 没有假送达
        assert event.last_error_code == "UNKNOWN_EVENT_TYPE"
    assert gateway.write_calls == 0


# ============================================================
# 3. 超时对账：重试先查再决定重发
# ============================================================


def test_timeout_retry_queries_write_result_before_resending(factory) -> None:
    task_id, result_id, attempt_id, event_id = _seed_intent(factory)
    gateway = _FakeGateway()
    gateway.fail_times = 1
    gateway.fail_code = ErrorCode.APPROVAL_API_TIMEOUT
    dispatcher = OutboxDispatcher(factory, gateway)

    t0 = utcnow()
    assert dispatcher.run_once(now=t0) is True

    event = _event(factory, event_id)
    assert event.event_status == OutboxStatus.PENDING.value
    assert event.attempt_no == 1
    assert event.last_error_code == ErrorCode.APPROVAL_API_TIMEOUT.value
    assert event.next_retry_at is not None
    # 指数退避：第一次重试等 base**1
    assert abs(
        (event.next_retry_at - t0).total_seconds() - backoff_seconds(1)
    ) < 1.0
    assert _attempt(factory, attempt_id).write_status == WriteStatus.WRITING.value

    # 对方其实已写入（客户端超时是假象）：把评论塞进"外部系统"
    key = _event(factory, event_id).idempotency_key
    gateway.comments.append(_Record("HT-1", "回写正文", key, "cmt-late"))

    later = t0 + timedelta(seconds=backoff_seconds(1) + 5)
    assert dispatcher.run_once(now=later) is True

    # 先查再决定：查到了 → 不重发，直接按成功记账
    assert gateway.get_calls == 1
    assert gateway.write_calls == 1
    event = _event(factory, event_id)
    assert event.event_status == OutboxStatus.DELIVERED.value
    assert _attempt(factory, attempt_id).write_status == WriteStatus.SUCCESS.value
    assert _task(factory, task_id).task_status == "done"
    assert len(gateway.comments) == 1


# ============================================================
# 4. 重试耗尽：failed + blocked 在 writeback，不重跑解析/规则
# ============================================================


def test_retry_exhaustion_fails_and_blocks_at_writeback(factory) -> None:
    task_id, result_id, attempt_id, event_id = _seed_intent(factory)
    with factory() as session:
        session.get(OutboxEvent, event_id).max_attempts = 2
        session.commit()

    gateway = _FakeGateway()
    gateway.always_fail = True  # 瞬时 5xx，永远失败
    dispatcher = OutboxDispatcher(factory, gateway)

    t0 = utcnow()
    assert dispatcher.run_once(now=t0) is True
    event = _event(factory, event_id)
    assert event.event_status == OutboxStatus.PENDING.value
    assert event.next_retry_at is not None  # 还有预算，退避等待

    later = t0 + timedelta(seconds=backoff_seconds(1) + 5)
    assert dispatcher.run_once(now=later) is True

    event = _event(factory, event_id)
    assert event.event_status == OutboxStatus.FAILED.value
    assert event.delivered_at is None
    assert event.last_error_code == ErrorCode.APPROVAL_API_ERROR.value

    attempt = _attempt(factory, attempt_id)
    assert attempt.write_status == WriteStatus.FAILED.value
    assert attempt.reason_code == ErrorCode.APPROVAL_API_ERROR.value

    task = _task(factory, task_id)
    assert task.write_status == WriteStatus.FAILED.value
    assert task.task_status == "blocked"
    assert task.blocked_stage == "writeback"
    assert task.last_error_code == ErrorCode.APPROVAL_API_ERROR.value

    # 阻塞在回写，不重跑解析与规则
    with factory() as session:
        assert session.execute(select(ContractParse)).scalars().first() is not None
        runs = list(session.execute(select(ReviewRun)).scalars())
        assert len(runs) == 1

    assert gateway.write_calls == 2  # 两次尝试，仅此而已


def test_permanent_error_fails_immediately_without_retry(factory) -> None:
    task_id, result_id, attempt_id, event_id = _seed_intent(factory)
    gateway = _FakeGateway()
    gateway.always_fail_permanent = True  # 实例不存在：重试不会改变结果
    dispatcher = OutboxDispatcher(factory, gateway)

    assert dispatcher.run_once() is True

    event = _event(factory, event_id)
    assert event.event_status == OutboxStatus.FAILED.value
    assert event.attempt_no == 1  # 没有浪费重试预算
    assert event.next_retry_at is None

    assert _attempt(factory, attempt_id).write_status == WriteStatus.FAILED.value
    task = _task(factory, task_id)
    assert task.task_status == "blocked"
    assert task.blocked_stage == "writeback"
    assert gateway.write_calls == 1


# ============================================================
# 5. 进程死亡窗口：租约互斥、到期回收、恰好一条评论
# ============================================================


def test_claim_lease_is_exclusive(factory) -> None:
    _task_id, _result_id, _attempt_id, event_id = _seed_intent(factory)
    t0 = utcnow()

    with factory() as session:
        first = claim_next_event(session, dispatcher_id="dispatcher-A", now=t0)
    assert first is not None and first.event_id == event_id

    with factory() as session:
        second = claim_next_event(session, dispatcher_id="dispatcher-B", now=t0)
    assert second is None  # A 持有租约，B 领不走同一事件


def test_expired_lease_is_recycled_and_reclaimable(factory) -> None:
    _task_id, _result_id, _attempt_id, event_id = _seed_intent(factory)
    t0 = utcnow()

    with factory() as session:
        claimed = claim_next_event(session, dispatcher_id="dispatcher-A", now=t0)
    assert claimed is not None and claimed.attempt_no == 1

    expired = t0 + timedelta(seconds=DEFAULT_LEASE_SECONDS + 5)
    with factory() as session:
        recycled = recycle_expired_leases(session, now=expired)
    assert recycled == [event_id]

    with factory() as session:
        reclaimed = claim_next_event(session, dispatcher_id="dispatcher-B", now=expired)
    assert reclaimed is not None
    assert reclaimed.event_id == event_id
    assert reclaimed.attempt_no == 2  # 死亡的那次也计入尝试次数


def test_kill_window_leaves_exactly_one_comment(factory) -> None:
    """领取后、记账前死亡，且外部其实已写入 —— 新派发器接管后评论恰好一条。"""
    task_id, _result_id, _attempt_id, event_id = _seed_intent(factory)
    gateway = _FakeGateway()
    t0 = utcnow()

    # 进程 A 领取后死亡（未派发、未记账）
    with factory() as session:
        claimed = claim_next_event(session, dispatcher_id="dead-A", now=t0)
    assert claimed is not None

    # A 其实已把评论写进了外部系统（死亡发生在写入之后、记账之前）
    gateway.comments.append(
        _Record("HT-1", "回写正文", claimed.idempotency_key, "cmt-ghost")
    )

    # 租约到期，新派发器接管并重发 —— 网关按幂等键去重
    expired = t0 + timedelta(seconds=DEFAULT_LEASE_SECONDS + 5)
    with factory() as session:
        recycle_expired_leases(session, now=expired)

    dispatcher = OutboxDispatcher(factory, gateway, dispatcher_id="dispatcher-B")
    assert dispatcher.run_once(now=expired + timedelta(seconds=1)) is True

    # 外部恰好一条评论（重发被幂等键挡下）
    assert len(gateway.comments) == 1
    assert gateway.write_calls == 1

    event = _event(factory, event_id)
    assert event.event_status == OutboxStatus.DELIVERED.value
    assert _task(factory, task_id).task_status == "done"


# ============================================================
# 6. 顺序：按入队顺序派发（FIFO）
# ============================================================


def test_events_are_dispatched_in_fifo_order(factory) -> None:
    first_ids = _seed_intent(factory, instance_id="HT-1")
    second_ids = _seed_intent(factory, instance_id="HT-2")
    gateway = _FakeGateway()
    dispatcher = OutboxDispatcher(factory, gateway)

    assert dispatcher.run_once() is True
    assert _event(factory, first_ids[3]).event_status == OutboxStatus.DELIVERED.value
    assert _event(factory, second_ids[3]).event_status == OutboxStatus.PENDING.value

    assert dispatcher.run_once() is True
    assert _event(factory, second_ids[3]).event_status == OutboxStatus.DELIVERED.value
