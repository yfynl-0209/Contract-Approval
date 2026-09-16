"""PG 幂等竞态（M9 Task 3）：并发重复请求返回**一个**效果，不泄漏 IntegrityError。

两个竞态面：
1. `save_review_result` —— 同批次同指纹并发保存；
2. `request_writeback` —— 同幂等键并发登记回写意图。

两者的正确行为：一方创建、另一方拿到**同一行**的幂等重放
（reused=True），而不是 IntegrityError 冒泡成 500。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import Actor
from app.enums import RunStatus, TaskStatus, WriteStatus
from app.models import ApprovalAttachment, ApprovalTask, ContractParse, ReviewResult, ReviewRun
from app.services.result_service import save_review_result
from app.services.writeback_service import request_writeback
from app.workflow.jobs import utcnow


def _actor(name: str) -> Actor:
    return Actor(actor_id=name, display_name=name, roles=frozenset(), tenant_id="default")


def _completed_run(session: Session) -> ReviewRun:
    """任务 → 附件 → 解析 →（已完成）批次，满足全部复合外键。"""
    task = ApprovalTask(
        provider="mock",
        tenant_id="default",
        instance_id="HT-IDEM",
        approval_code="HT-IDEM-0001",
        task_status=TaskStatus.REVIEWING.value,
        # ⚠️ 回写门禁要求立场上下文可信（否则 CONTEXT_NOT_VALID 拒绝——
        # 拒绝行也占幂等键，但那是"拒绝语义"不是我们要测的写入竞态）
        context_source="approval_system",
        context_status="confirmed",
    )
    session.add(task)
    session.flush()
    attachment = ApprovalAttachment(
        task_id=task.id,
        attachment_id="ATT-1",
        file_name="a.pdf",
        object_key="k",
        file_checksum="0" * 64,
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
        run_status=RunStatus.COMPLETED.value,
    )
    session.add(run)
    session.commit()
    return run


# ------------------------------------------------------------
# 结果保存竞态
# ------------------------------------------------------------


def test_pg_concurrent_identical_saves_return_one_effect(pg_session, pg_sessionmaker) -> None:
    run = _completed_run(pg_session)
    # ⚠️ 线程里只传**普通值**：run 是主线程会话的对象，commit 后已过期，
    # 在线程里访问 run.id 会触发跨线程懒加载（Session 不允许并发操作）
    run_id: int = run.id

    def save_same(_: str):
        session = pg_sessionmaker()
        try:
            saved = save_review_result(
                session,
                run_id=run_id,
                overall_risk_level="low",
                summary_text="汇总",
                focus_points_json=[],
                comment_text="同一份正文",
                actor=_actor("same"),
            )
            # ⚠️ save_review_result 明确"不提交"（调用方决定事务边界）——
            # 不提交的话 close() 会回滚，竞态根本不会发生（第一个行消失，
            # 第二个"成功"）—— 竞态测试必须像生产一样**真的提交**
            session.commit()
            return saved
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save_same, ["a", "b"]))

    ids = {r.result_id for r in results}
    assert len(ids) == 1, f"同指纹并发保存应落到同一行，实际 {results}"
    assert sorted(r.reused for r in results) == [False, True], (
        f"恰好一方创建、一方复用：{[r.reused for r in results]}"
    )
    count = pg_session.execute(
        select(func.count()).select_from(ReviewResult)
    ).scalar_one()
    assert count == 1, "重复内容不得产生第二行"


# ------------------------------------------------------------
# 回写意图竞态
# ------------------------------------------------------------


def _confirmed_result(session: Session, run: ReviewRun) -> ReviewResult:
    """一条已人工确认的结果（回写门禁的放行前提）。"""
    saved = save_review_result(
        session,
        run_id=run.id,
        overall_risk_level="low",
        summary_text="汇总",
        focus_points_json=[],
        comment_text="审查意见",
        actor=_actor("confirm"),
    )
    result = session.get(ReviewResult, saved.result_id)
    result.manual_confirmed = 1
    result.confirmed_by = "confirm"
    result.confirmed_at = utcnow()
    # confirmed_digest 必须与确认时的 content_digest 一致（确认有效性判据）
    result.confirmed_digest = result.content_digest
    session.commit()
    return result


def test_pg_concurrent_writeback_requests_return_one_attempt(
    pg_session, pg_sessionmaker
) -> None:
    run = _completed_run(pg_session)
    result = _confirmed_result(pg_session, run)
    task = pg_session.get(ApprovalTask, run.task_id)
    task.task_status = TaskStatus.DONE.value
    pg_session.commit()
    # ⚠️ 同上：线程里只传普通值，避免跨线程懒加载
    instance_id: str = task.instance_id
    result_id: int = result.id

    def request(_: str):
        session = pg_sessionmaker()
        try:
            ref = request_writeback(
                session,
                instance_id=instance_id,
                result_id=result_id,
                actor=_actor("w"),
            )
            session.commit()  # 同上：意图必须真的落库，竞态才会发生
            return ref
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        refs = list(pool.map(request, ["a", "b"]))

    attempt_ids = {ref.attempt_id for ref in refs}
    assert len(attempt_ids) == 1, f"并发同键回写应指向同一次尝试：{refs}"
    assert all(ref.write_status == WriteStatus.WRITING.value for ref in refs), refs
    assert sum(1 for ref in refs if ref.reused) == 1, (
        f"恰好一方创建、一方重放：{[(r.attempt_id, r.reused) for r in refs]}"
    )
