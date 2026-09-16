"""结果持久化服务的测试（M6 / Task 2）。

## 本文件守住的四类"写错了也不报错"

1. **调用方拿旧口径保存新批次** —— `overall_risk_level` 与 M5 聚合不一致：
   两份口径焊进同一条记录，此后没人能回答"这条结果到底是哪个等级"。
   必须以**稳定错误码**（`RESULT_INPUT_MISMATCH`）拒绝，而不是 500 或静默改值。
2. **摘要 / 关注点 / 正文在落库路上变形** —— 编码、规范化、JSON 序列化
   任何一步改了字节，确认时绑定的 `content_digest` 就对不上"当时要写的正文"。
3. **重放被当成新版本**（或反之）—— 幂等判据是 `result_fingerprint`：
   指纹相同必须**复用**，指纹不同必须**新版本**且历史保留。
4. **聚合口径字段被漏写** —— 风险等级 / 完整性 / 三个计数少写一个，
   查询侧只能退回现算，而"结果表"就名存实亡。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.auth import Actor
from app.config import PROJECT_ROOT
from app.enums import ReasonCode, RiskLevel
from app.models import ReviewResult, ReviewRule
from app.rules.applicability import ReviewContext
from app.rules.aggregator import aggregate
from app.rules.evaluator import RuleEvaluation as Evaluation
from app.schemas import SaveReviewResultRequest
from app.services.result_service import (
    ResultInputError,
    confirmation_valid,
    confirm_result,
    get_result_view,
    save_review_result,
)
from app.services.rule_service import (
    EvaluationStatus,
    load_active_rules,
    record_evaluations,
    start_run,
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
    """独立库：用**交付的 schema.sql** 建表（同 test_rule_service 的约定）。"""
    path = work_dir / "results.db"
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


def _add_rule(session: Session, code: str, **overrides: object) -> None:
    kwargs: dict[str, object] = {
        "rule_code": code,
        "rule_name": f"{code} 的名称",
        "rule_category": "自动续约",
        "risk_level": "medium",
        "rule_status": "active",
        "priority": 10,
        "rule_version": 1,
        "match_mode": "keyword",
        "match_text": '{"keywords": ["自动续约"]}',
    }
    kwargs.update(overrides)
    session.add(ReviewRule(**kwargs))  # type: ignore[arg-type]
    session.flush()


def _three_rules(session: Session) -> None:
    _add_rule(session, "R_A", priority=10)
    _add_rule(session, "R_B", priority=20, match_text='{"keywords": ["保密"]}')
    _add_rule(session, "R_C", priority=30, risk_level="high")


def _start(session: Session):
    return start_run(
        session,
        task_id=1,
        parse_id=1,
        context=ReviewContext(
            contract_type="procurement",
            our_contract_label="party_a",
            our_business_role="buyer",
        ),
        rules=load_active_rules(session),
        model_version="mock:deterministic",
        prompt_version="v1",
        config_version="engine-v1",
    )


def _evaluation(code: str) -> Evaluation:
    return Evaluation(
        rule_code=code,
        rule_version=1,
        status=EvaluationStatus.HIT,
        risk_level=RiskLevel.MEDIUM if code != "R_C" else RiskLevel.HIGH,
        reason_code=ReasonCode.CONDITION_MATCHED,
        reason_text=f"{code} 的说明",
        evidence_text="证据片段",
        evidence_json=json.dumps(
            [{"text": "证据片段", "position": {"page": 1}}], ensure_ascii=False
        ),
        hit_detail={},
    )


def _completed_run(session: Session):
    """建一个**已完成**的批次：三条规则全命中，R_C 为 high → 聚合 high。"""
    _three_rules(session)
    run = _start(session)
    rules = load_active_rules(session)
    evaluations = [_evaluation("R_A"), _evaluation("R_B"), _evaluation("R_C")]
    record_evaluations(
        session, run=run, task_id=1, rules=rules, evaluations=evaluations
    )
    from app.services.rule_service import complete_run

    agg = aggregate(evaluations)
    complete_run(session, run=run, aggregate=agg)
    session.flush()
    return run, agg


def _result_count(session: Session) -> int:
    return session.execute(select(func.count()).select_from(ReviewResult)).scalar_one()


# ============================================================
# 1. 入口防线：批次状态与口径一致
# ============================================================


def test_save_rejects_a_run_that_is_not_completed(session: Session) -> None:
    """未完成的批次没有完整聚合口径 —— 保存它等于把半截结论焊成正式版。"""
    _three_rules(session)
    run = _start(session)  # running，未 complete

    with pytest.raises(ResultInputError) as info:
        save_review_result(
            session,
            run_id=run.run_id,
            overall_risk_level="high",
            summary_text="摘要",
            focus_points_json=["关注点"],
            comment_text="正文",
            actor=_actor("reviewer-1"),
        )
    assert info.value.reason_code == "RESULT_RUN_NOT_COMPLETED"
    assert _result_count(session) == 0

    session.rollback()


def test_save_rejects_an_unknown_run_with_a_stable_code(session: Session) -> None:
    with pytest.raises(ResultInputError) as info:
        save_review_result(
            session,
            run_id=999,
            overall_risk_level="high",
            summary_text="摘要",
            focus_points_json=[],
            comment_text="正文",
            actor=_actor("reviewer-1"),
        )
    assert info.value.reason_code == "RESULT_NOT_FOUND"


def test_supplied_risk_level_must_equal_the_aggregate(session: Session) -> None:
    """**M6 Task 2 核心断言**：调用方传入的风险等级必须与 M5 聚合一致。

    错误码稳定为 `RESULT_INPUT_MISMATCH`：调用方（可能是模型）要能凭它
    修正后重试，而不是对着 500 猜。拒绝后**不得留下任何行**。
    """
    run, agg = _completed_run(session)
    assert agg.overall_risk_level.value == "high"

    with pytest.raises(ResultInputError) as info:
        save_review_result(
            session,
            run_id=run.run_id,
            overall_risk_level="low",  # 与聚合不符
            summary_text="摘要",
            focus_points_json=["关注点"],
            comment_text="正文",
            actor=_actor("reviewer-1"),
        )
    assert info.value.reason_code == "RESULT_INPUT_MISMATCH"
    assert _result_count(session) == 0

    # 一致的值可以通过（错误在 DML 之前抛出，会话无需回滚）
    saved = save_review_result(
        session,
        run_id=run.run_id,
        overall_risk_level="high",
        summary_text="摘要",
        focus_points_json=["关注点"],
        comment_text="正文",
        actor=_actor("reviewer-1"),
    )
    assert saved.overall_risk_level == "high"


# ============================================================
# 2. 单行持久化：聚合口径 + 内容 + 摘要
# ============================================================


def test_persists_aggregate_and_content_in_one_row(session: Session) -> None:
    """聚合口径（等级 / 完整性 / 三计数）、摘要、关注点、正文、来源批次、
    操作者、版本 —— 一次保存全部落进 `review_results` 的同一行。"""
    run, agg = _completed_run(session)

    saved = save_review_result(
        session,
        run_id=run.run_id,
        overall_risk_level="high",
        summary_text="摘要内容",
        focus_points_json=["第一关注点", "第二关注点"],
        comment_text="回写正文",
        actor=_actor("reviewer-1"),
    )

    row = session.get(ReviewResult, saved.result_id)
    assert row is not None

    # 聚合口径（来自 M5 聚合，而不是调用方自由发挥）
    assert row.overall_risk_level == agg.overall_risk_level.value
    assert row.review_status == agg.review_status.value
    assert row.hit_count == agg.counts["hit"]
    assert row.needs_review_count == agg.counts["needs_review"]
    assert row.not_applicable_count == agg.counts["not_applicable"]

    # 内容三件套
    assert row.summary_text == "摘要内容"
    assert json.loads(row.focus_points_json) == ["第一关注点", "第二关注点"]
    assert row.comment_text == "回写正文"
    # content_digest = 规范化 UTF-8 正文的 SHA-256（确认绑定的是"这份"正文）
    assert row.content_digest == hashlib.sha256(
        "回写正文".encode("utf-8")
    ).hexdigest()

    # 来源与留痕
    assert row.run_id == run.run_id
    assert row.created_by == "reviewer-1"
    assert row.version_no == 1
    assert row.supersedes_result_id is None


# ============================================================
# 3. 幂等与版本化
# ============================================================


def test_replaying_identical_content_reuses_the_result(session: Session) -> None:
    """同批次同指纹 → **复用**：不新建版本、不覆盖、不重复计数。"""
    run, _ = _completed_run(session)
    kwargs = {
        "run_id": run.run_id,
        "overall_risk_level": "high",
        "summary_text": "摘要",
        "focus_points_json": ["关注点"],
        "comment_text": "版本一正文",
        "actor": _actor("reviewer-1"),
    }

    first = save_review_result(session, **kwargs)  # type: ignore[arg-type]
    replay = save_review_result(session, **kwargs)  # type: ignore[arg-type]

    assert first.reused is False
    assert replay.reused is True
    assert replay.result_id == first.result_id
    assert replay.version_no == first.version_no == 1
    assert _result_count(session) == 1


def test_changed_content_creates_a_new_version_and_keeps_history(
    session: Session,
) -> None:
    """内容变化 → 新版本接替旧版本，**历史必须保留**。

    审计口径是"当时确认的是哪一版"：覆盖历史会让
    "已确认 v1，但 v1 被删了"变成无法解释的状态。
    """
    run, _ = _completed_run(session)

    first = save_review_result(
        session,
        run_id=run.run_id,
        overall_risk_level="high",
        summary_text="摘要",
        focus_points_json=["关注点"],
        comment_text="版本一正文",
        actor=_actor("reviewer-1"),
    )
    second = save_review_result(
        session,
        run_id=run.run_id,
        overall_risk_level="high",
        summary_text="摘要",
        focus_points_json=["关注点"],
        comment_text="版本二正文",  # 内容变化
        actor=_actor("reviewer-1"),
    )

    assert second.reused is False
    assert second.version_no == 2
    assert second.result_id != first.result_id

    row2 = session.get(ReviewResult, second.result_id)
    assert row2 is not None
    assert row2.supersedes_result_id == first.result_id

    # 历史保留：v1 仍在库里
    assert session.get(ReviewResult, first.result_id) is not None
    assert _result_count(session) == 2


# ============================================================
# 4. 结果视图
# ============================================================


def test_get_result_view_reports_version_and_confirmation_state(
    session: Session,
) -> None:
    """视图必须能回答：这是不是当前版本、确认是否有效。"""
    run, _ = _completed_run(session)

    first = save_review_result(
        session,
        run_id=run.run_id,
        overall_risk_level="high",
        summary_text="摘要",
        focus_points_json=["关注点"],
        comment_text="版本一正文",
        actor=_actor("reviewer-1"),
    )

    view = get_result_view(session, result_id=first.result_id)
    assert view.result_id == first.result_id
    assert view.run_id == run.run_id
    assert view.version_no == 1
    assert view.is_current_version is True
    assert view.manual_confirmed is False
    assert view.confirmation_valid is False  # 未确认

    # 新版本出现后：旧版本不再是当前版本，其"确认"自然失效
    save_review_result(
        session,
        run_id=run.run_id,
        overall_risk_level="high",
        summary_text="摘要",
        focus_points_json=["关注点"],
        comment_text="版本二正文",
        actor=_actor("reviewer-1"),
    )
    old_view = get_result_view(session, result_id=first.result_id)
    assert old_view.is_current_version is False
    assert old_view.confirmation_valid is False


def test_get_result_view_rejects_unknown_result(session: Session) -> None:
    with pytest.raises(ResultInputError) as info:
        get_result_view(session, result_id=999)
    assert info.value.reason_code == "RESULT_NOT_FOUND"


# ============================================================
# 6. 结果确认与失效（Task 3）
# ============================================================


def _save_one(session: Session, *, comment_text: str = "版本一正文"):
    run, _ = _completed_run(session)
    return save_review_result(
        session,
        run_id=run.run_id,
        overall_risk_level="high",
        summary_text="摘要",
        focus_points_json=["关注点"],
        comment_text=comment_text,
        actor=_actor("reviewer-1"),
    )


def test_confirm_result_binds_the_backend_digest_and_retains_the_actor(
    session: Session,
) -> None:
    """确认 = 把 `confirmed_digest` 绑定到**当时的** `content_digest`。

    摘要由后端计算（浏览器 / 调用方**没有**传摘要的入口）：
    让前端传，等于让"确认了哪份正文"变成可以伪造的事实。
    """
    saved = _save_one(session)

    view = confirm_result(session, result_id=saved.result_id, actor=_actor("reviewer-9"))

    assert view.manual_confirmed is True
    assert view.confirmed_by == "reviewer-9"
    assert view.confirmed_at is not None
    assert view.confirmed_digest == view.content_digest
    assert view.confirmation_valid is True

    # 签名层面就不存在 digest 入参（结构性防线，不靠运行时校验）
    import inspect

    params = inspect.signature(confirm_result).parameters
    assert "digest" not in params and "confirmed_digest" not in params

    # 库里的绑定与后端计算一致
    row = session.get(ReviewResult, saved.result_id)
    assert row is not None
    assert row.confirmed_digest == hashlib.sha256(
        "版本一正文".encode("utf-8")
    ).hexdigest()


def test_repeated_confirmation_is_idempotent(session: Session) -> None:
    """重复确认是幂等 no-op：不换时间、不换人、**不追加审计事件**。

    审计账按"发生过的事"记账：同一个人对同一版本确认两次，
    是一次业务事实，不是两次。
    """
    saved = _save_one(session)

    first = confirm_result(session, result_id=saved.result_id, actor=_actor("reviewer-9"))
    again = confirm_result(session, result_id=saved.result_id, actor=_actor("reviewer-9"))

    assert again.confirmation_valid is True
    assert again.confirmed_at == first.confirmed_at
    assert again.confirmed_by == first.confirmed_by

    from app.models import AuditEvent

    events = session.execute(select(AuditEvent)).scalars().all()
    assert len([e for e in events if e.action == "RESULT_CONFIRMED"]) == 1


def test_confirming_a_missing_result_is_a_stable_error(session: Session) -> None:
    with pytest.raises(ResultInputError) as info:
        confirm_result(session, result_id=999, actor=_actor("reviewer-9"))
    assert info.value.reason_code == "RESULT_NOT_FOUND"


def test_confirmation_writes_an_immutable_audit_event(session: Session) -> None:
    """审计事件只放标识与摘要：能回答"谁在何时确认了哪一版"，
    但**不含**正文 / 指针类字段。"""
    from app.models import AuditEvent

    saved = _save_one(session)
    confirm_result(session, result_id=saved.result_id, actor=_actor("reviewer-9"))

    events = [
        e for e in session.execute(select(AuditEvent)).scalars().all()
        if e.action == "RESULT_CONFIRMED"
    ]
    assert len(events) == 1

    event = events[0]
    assert event.task_id is not None
    assert event.actor_name == "reviewer-9"
    assert event.target_type == "review_result"
    assert event.target_id == saved.result_id

    detail = json.loads(event.detail_json or "{}")
    assert detail["result_id"] == saved.result_id
    assert detail["version_no"] == saved.version_no
    assert "content_digest" in detail
    # 敏感字段不进审计
    assert "comment_text" not in detail
    assert "file_path" not in detail
    assert "object_key" not in detail


def test_new_version_invalidates_the_old_confirmation(session: Session) -> None:
    """**Task 3 核心断言**：新版本出现 → 旧版本的确认失效。

    失效是**观察到的事实**而不是改写历史：
    旧版本行上的确认字段原样保留（manual_confirmed / confirmed_digest 不动），
    审计事件也不删除 —— 只是"当前版本"已经不是它。
    """
    run, _ = _completed_run(session)
    first = save_review_result(
        session,
        run_id=run.run_id,
        overall_risk_level="high",
        summary_text="摘要",
        focus_points_json=["关注点"],
        comment_text="版本一正文",
        actor=_actor("reviewer-1"),
    )
    confirm_result(session, result_id=first.result_id, actor=_actor("reviewer-9"))
    assert confirmation_valid(session, result_id=first.result_id) is True

    # 内容变化 → 新版本
    second = save_review_result(
        session,
        run_id=run.run_id,
        overall_risk_level="high",
        summary_text="摘要",
        focus_points_json=["关注点"],
        comment_text="版本二正文",
        actor=_actor("reviewer-1"),
    )

    # 旧版本确认失效，且历史与审计原样保留
    assert confirmation_valid(session, result_id=first.result_id) is False
    old_row = session.get(ReviewResult, first.result_id)
    assert old_row is not None
    assert old_row.manual_confirmed == 1
    assert old_row.confirmed_digest is not None

    from app.models import AuditEvent

    assert len(
        [
            e
            for e in session.execute(select(AuditEvent)).scalars().all()
            if e.action == "RESULT_CONFIRMED"
        ]
    ) == 1  # v1 的确认审计仍在

    # 新版本尚未确认
    assert confirmation_valid(session, result_id=second.result_id) is False


def test_changed_comment_body_breaks_the_digest_binding(session: Session) -> None:
    """同版本正文被改（理论旁路）后，确认摘要与正文不一致 → 失效。"""
    saved = _save_one(session)
    confirm_result(session, result_id=saved.result_id, actor=_actor("reviewer-9"))

    # 直接篡改库里的正文（模拟旁路写入），确认绑定必须暴露不一致
    row = session.get(ReviewResult, saved.result_id)
    assert row is not None
    row.comment_text = "被篡改的正文"
    row.content_digest = hashlib.sha256("被篡改的正文".encode("utf-8")).hexdigest()
    session.flush()

    assert confirmation_valid(session, result_id=saved.result_id) is False


# ============================================================
# 5. 工具 6 请求 schema（严格校验）
# ============================================================


def test_save_review_result_request_schema_is_strict() -> None:
    """拒绝未知字段 + 去除空白（同 ToolRequest 家族约定）。"""
    request = SaveReviewResultRequest.model_validate(
        {
            "run_id": 1,
            "overall_risk_level": "high",
            "summary_text": "  摘要  ",
            "focus_points": ["关注点"],
            "comment_text": "正文",
        }
    )
    assert request.summary_text == "摘要"
    assert request.focus_points == ["关注点"]

    with pytest.raises(ValidationError):
        SaveReviewResultRequest.model_validate(
            {
                "run_id": 1,
                "overall_risk_level": "high",
                "summary_text": "摘要",
                "focus_points": [],
                "comment_text": "正文",
                "unexpected": "拼错的键",
            }
        )

    with pytest.raises(ValidationError):
        SaveReviewResultRequest.model_validate(
            {
                "run_id": 1,
                "overall_risk_level": "high",
                "summary_text": "   ",  # 空白不是摘要
                "focus_points": [],
                "comment_text": "正文",
            }
        )
