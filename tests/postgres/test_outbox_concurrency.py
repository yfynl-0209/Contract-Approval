"""PG 并发派发语义（M9 Task 3）：两个派发器不能领同一个事件。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.enums import OutboxEventType, OutboxStatus
from app.models import ApprovalTask, OutboxEvent
from app.outbox import claim_next_event


def test_pg_two_dispatchers_never_claim_the_same_event(pg_session, pg_sessionmaker) -> None:
    """8 个事件 × 8 个并发派发器：每个事件恰好被领一次。"""
    task = ApprovalTask(
        provider="mock",
        tenant_id="default",
        instance_id="HT-OUTBOX",
        approval_code="HT-OUTBOX-0001",
    )
    pg_session.add(task)
    pg_session.flush()
    events = [
        OutboxEvent(
            aggregate_type="task",
            aggregate_id=task.id,
            event_type=OutboxEventType.WRITE_APPROVAL_COMMENT.value,
            payload_json='{"comment": "c"}',
            idempotency_key=f"ob:{task.id}:{index}",
            event_status=OutboxStatus.PENDING.value,
            attempt_no=0,
            max_attempts=3,
        )
        for index in range(8)
    ]
    pg_session.add_all(events)
    pg_session.commit()
    event_ids = [event.id for event in events]

    def grab(dispatcher_id: str):
        session = pg_sessionmaker()
        try:
            return claim_next_event(session, dispatcher_id=dispatcher_id)
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(grab, [f"d-{i}" for i in range(8)]))

    claimed = [r.event_id for r in results if r is not None]
    assert len(claimed) == len(set(claimed)), f"同一事件被领取多次：{sorted(claimed)}"
    assert set(claimed) == set(event_ids), (
        f"SKIP LOCKED 下不应有事件被漏领：{set(event_ids) - set(claimed)}"
    )

    # 每个被领走的事件：租约归属唯一、attempt_no 恰好 +1
    # ⚠️ pg_session 的身份映射还是播种时的旧对象 —— 先过期，强制重读
    pg_session.expire_all()
    for event in pg_session.execute(select(OutboxEvent)).scalars():
        assert event.attempt_no == 1
        assert event.lease_owner is not None
