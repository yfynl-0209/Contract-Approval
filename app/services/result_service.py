"""结果持久化（M6 / Task 2）：M5 聚合 → `review_results` 的唯一写入口。

## 深模块边界

外界只需要 `save_review_result(...)`：批次状态校验、聚合读取与口径比对、
指纹计算、版本链维护、幂等复用全部封装在内。`review_results` 不接受
任何旁路写入（聚合结论"现算不落库"的 M5 边界由此翻页，翻页的入口只有这一个）。

## 两个摘要的分工（容易混）

- `content_digest`：**回写正文**的 SHA-256 —— 人工确认绑定的是"这份正文"。
- `result_fingerprint`：规范化(摘要, 关注点, 正文, 聚合口径, 批次身份) 的
  SHA-256 —— "这份结果的内容与输入是否与另一份完全一致"（重放复用判据）。
  合并两者会让"正文没变但规则输入变了"的新版本被误判为可复用旧版本。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import Actor
from app.context import get_correlation_id
from app.enums import AuditAction, RunStatus
from app.models import AuditEvent, ReviewResult, ReviewRun
from app.rules.aggregator import Aggregate
from app.services.rule_service import aggregate_of_run


class ResultInputError(ValueError):
    """保存 / 查看结果时的稳定业务错误。

    `reason_code` 稳定：调用方（含模型侧工具实现）凭它决定重试还是放弃，
    不能拿 `ValueError` 的自由文本当判据 —— 后者改个措辞调用方就断了。
    """

    #: 目标对象（批次 / 结果）不存在
    RESULT_NOT_FOUND = "RESULT_NOT_FOUND"
    #: 批次未完成：聚合口径不完整，保存等于把半截结论焊成正式版
    RESULT_RUN_NOT_COMPLETED = "RESULT_RUN_NOT_COMPLETED"
    #: 调用方传入的风险等级与 M5 聚合不一致
    RESULT_INPUT_MISMATCH = "RESULT_INPUT_MISMATCH"

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class SavedResult:
    """一次保存的结果：`reused=True` 表示命中幂等复用，没有新建版本。"""

    result_id: int
    run_id: int
    task_id: int
    version_no: int
    reused: bool
    overall_risk_level: str
    content_digest: str


@dataclass(frozen=True)
class ResultView:
    """结果视图：能回答"这是不是当前版本、确认是否有效"。"""

    result_id: int
    run_id: int
    task_id: int
    version_no: int
    is_current_version: bool
    #: 确认有效 = 已确认 **且** 仍是当前版本 **且** 确认时绑定的摘要与当前正文一致。
    #: 内容变化产生新版本后，旧版本的确认自然失效（Task 3 的失效口径）。
    confirmation_valid: bool
    overall_risk_level: str
    review_status: str
    hit_count: int
    needs_review_count: int
    not_applicable_count: int
    summary_text: str
    focus_points: list[str]
    comment_text: str
    content_digest: str
    manual_confirmed: bool
    confirmed_by: str | None
    confirmed_at: datetime | None
    confirmed_digest: str | None
    supersedes_result_id: int | None
    created_by: str | None
    created_at: datetime | None
    updated_at: datetime | None


def result_fingerprint_of(
    run: ReviewRun,
    *,
    overall_risk_level: str,
    review_status: str,
    counts: dict[str, int],
    summary_text: str,
    focus_points: list[str],
    comment_text: str,
) -> str:
    """结果指纹：规范化(摘要, 关注点, 正文, 聚合口径, 批次身份) 的 SHA-256。

    批次身份含 parse_id 与 ruleset/model/prompt/config 版本 ——
    六项输入的任何变化都换指纹；指纹相同 = 内容与输入完全一致。
    """
    payload = {
        # 批次身份（批次行本身就是六项输入的产物）
        "run": {
            "run_id": run.id,
            "task_id": run.task_id,
            "version_no": run.version_no,
            "parse_id": run.parse_id,
            "ruleset_version": run.ruleset_version,
            "model_version": run.model_version,
            "prompt_version": run.prompt_version,
            "config_version": run.config_version,
        },
        # 聚合口径（按 key 排序，摆脱 dict 顺序）
        "aggregate": {
            "overall_risk_level": overall_risk_level,
            "review_status": review_status,
            "counts": {key: counts[key] for key in sorted(counts)},
        },
        # 内容三件套
        "content": {
            "summary_text": summary_text,
            "focus_points": list(focus_points),
            "comment_text": comment_text,
        },
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _counts_of(aggregate: Aggregate) -> tuple[int, int, int]:
    """聚合计数 → (hit, needs_review, not_applicable)。缺键按 0 处理。"""
    return (
        aggregate.counts.get("hit", 0),
        aggregate.counts.get("needs_review", 0),
        aggregate.counts.get("not_applicable", 0),
    )


def save_review_result(
    session: Session,
    *,
    run_id: int,
    overall_risk_level: str,
    summary_text: str,
    focus_points_json: list[str],
    comment_text: str,
    actor: Actor,
) -> SavedResult:
    """把一份审查结果保存进 `review_results`。**不提交**（调用方决定事务边界）。

    口径规则：
    - `overall_risk_level` 由调用方传入但**必须**等于 M5 聚合
      （不等 → `RESULT_INPUT_MISMATCH`，稳定错误码）；
    - 聚合口径字段（等级 / 完整性 / 三计数）一律取自聚合，调用方说了不算；
    - 同批次同指纹 → **复用**（`reused=True`）；内容或输入变化 → 新版本，
      `supersedes_result_id` 接替当前最大版本，历史保留。

    Raises:
        ResultInputError: 批次不存在 / 未完成 / 口径不一致。
    """
    run = session.get(ReviewRun, run_id)
    if run is None:
        raise ResultInputError(
            ResultInputError.RESULT_NOT_FOUND, f"批次 {run_id} 不存在"
        )
    if run.run_status != RunStatus.COMPLETED.value:
        raise ResultInputError(
            ResultInputError.RESULT_RUN_NOT_COMPLETED,
            f"批次 {run_id} 尚未完成（当前 {run.run_status!r}），"
            "聚合口径不完整，不能保存正式结果",
        )

    aggregate = aggregate_of_run(session, run_id)
    if overall_risk_level != aggregate.overall_risk_level.value:
        raise ResultInputError(
            ResultInputError.RESULT_INPUT_MISMATCH,
            f"传入风险等级 {overall_risk_level!r} 与批次聚合 "
            f"{aggregate.overall_risk_level.value!r} 不一致 —— "
            "调用方必须以聚合口径为准修正后重试",
        )

    content_digest = hashlib.sha256(comment_text.encode("utf-8")).hexdigest()
    fingerprint = result_fingerprint_of(
        run,
        overall_risk_level=aggregate.overall_risk_level.value,
        review_status=aggregate.review_status.value,
        counts=aggregate.counts,
        summary_text=summary_text,
        focus_points=list(focus_points_json),
        comment_text=comment_text,
    )

    # 幂等复用：同批次同指纹（内容与输入完全一致）
    existing = session.execute(
        select(ReviewResult).where(
            ReviewResult.run_id == run_id,
            ReviewResult.result_fingerprint == fingerprint,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return SavedResult(
            result_id=existing.id,
            run_id=run_id,
            task_id=existing.task_id,
            version_no=existing.version_no,
            reused=True,
            overall_risk_level=existing.overall_risk_level,
            content_digest=existing.content_digest,
        )

    # 新版本：任务内最大版本 + 1，显式接替当前最大版本
    latest = session.execute(
        select(ReviewResult)
        .where(ReviewResult.task_id == run.task_id)
        .order_by(ReviewResult.version_no.desc())
        .limit(1)
    ).scalar_one_or_none()
    hit_count, needs_review_count, not_applicable_count = _counts_of(aggregate)

    row = ReviewResult(
        task_id=run.task_id,
        run_id=run_id,
        overall_risk_level=aggregate.overall_risk_level.value,
        review_status=aggregate.review_status.value,
        hit_count=hit_count,
        needs_review_count=needs_review_count,
        not_applicable_count=not_applicable_count,
        summary_text=summary_text,
        focus_points_json=json.dumps(list(focus_points_json), ensure_ascii=False),
        comment_text=comment_text,
        content_digest=content_digest,
        result_fingerprint=fingerprint,
        version_no=(latest.version_no + 1) if latest is not None else 1,
        supersedes_result_id=latest.id if latest is not None else None,
        # `created_by` 是**显示名**（这一列是给人看的）。
        # 机器判据用 `actor_id`，它落在审计事件里 —— 见 `confirm_result`。
        created_by=actor.display_name,
    )
    session.add(row)
    session.flush()

    return SavedResult(
        result_id=row.id,
        run_id=run_id,
        task_id=run.task_id,
        version_no=row.version_no,
        reused=False,
        overall_risk_level=row.overall_risk_level,
        content_digest=content_digest,
    )


def _latest_version_of(session: Session, task_id: int) -> int:
    return session.execute(
        select(func.max(ReviewResult.version_no)).where(
            ReviewResult.task_id == task_id
        )
    ).scalar_one()


def confirmation_valid(session: Session, *, result_id: int) -> bool:
    """确认有效性（后端计算，Fixed Decision 3）。

    = 已确认（`manual_confirmed` 且 `confirmed_digest == content_digest`）
      **且** 仍是任务的当前版本 —— 新版本出现后，旧版本上的确认字段
      原样保留（历史不删），但"当前结果"已不是它，确认自然失效。

    ⚠️ 摘要绑定在后端完成：调用方（含浏览器）没有传摘要的入口，
    "确认了哪份正文"因此不是可以伪造的事实。
    """
    row = session.get(ReviewResult, result_id)
    if row is None:
        return False
    if not bool(row.manual_confirmed) or row.confirmed_digest != row.content_digest:
        return False
    return is_current_version(session, result_id=result_id)


def is_current_version(session: Session, *, result_id: int) -> bool:
    """该结果是否仍是**任务的当前版本**。

    单独暴露它，是因为"列表里哪几行是当前版本"与"确认还有效吗"
    必须用**同一判据**。两处各写一遍时的分叉表现是：同一个页面上
    "列表说它是当前版本、详情说它已被接替"，而两个数字各自看起来都对
    （M8 的 G-3 缺口正是这一类）。

    结果不存在时返回 `False`（调用方要区分"不存在"应自己先查）。
    """
    row = session.get(ReviewResult, result_id)
    if row is None:
        return False
    return row.version_no == _latest_version_of(session, row.task_id)


def confirm_result(session: Session, *, result_id: int, actor: Actor) -> ResultView:
    """人工确认一份结果：绑定确认摘要、留痕确认人，并写入不可变审计事件。

    **不提交**（调用方决定事务边界）。重复确认是幂等 no-op：
    不换时间、不换人、不追加审计事件 —— 同一人对同一版本确认两次
    是一次业务事实，不是两次。

    Raises:
        ResultInputError: 结果不存在。
    """
    row = session.get(ReviewResult, result_id)
    if row is None:
        raise ResultInputError(
            ResultInputError.RESULT_NOT_FOUND, f"结果 {result_id} 不存在"
        )

    already = (
        bool(row.manual_confirmed) and row.confirmed_digest == row.content_digest
    )
    if not already:
        row.manual_confirmed = 1
        row.confirmed_by = actor.display_name
        # 与 SQLite CURRENT_TIMESTAMP 同为 UTC（naive），不混用本地时区
        row.confirmed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        # 绑定到后端计算的当前摘要 —— 没有（也不允许有）调用方传入的摘要
        row.confirmed_digest = row.content_digest

        session.add(
            AuditEvent(
                task_id=row.task_id,
                # ⚠️ `actor_id` 与 `actor_name` **两个都写**：只有名字时，
                # "张伟"在两个租户/两个部门各有一个，事后无法区分是谁确认的 ——
                # 而审计的全部价值就在于事后能确定这一点。
                actor_id=actor.actor_id,
                actor_name=actor.display_name,
                action=AuditAction.RESULT_CONFIRMED.value,
                target_type="review_result",
                target_id=row.id,
                correlation_id=get_correlation_id(),
                # 只放标识与摘要，不放正文 / 指针类字段
                detail_json=json.dumps(
                    {
                        "result_id": row.id,
                        "run_id": row.run_id,
                        "version_no": row.version_no,
                        "content_digest": row.content_digest,
                    },
                    ensure_ascii=False,
                ),
            )
        )
        session.flush()

    return get_result_view(session, result_id=result_id)


def get_result_view(session: Session, *, result_id: int) -> ResultView:
    """读取一份结果（含版本与确认状态）。只读，不写。

    Raises:
        ResultInputError: 结果不存在。
    """
    row = session.get(ReviewResult, result_id)
    if row is None:
        raise ResultInputError(
            ResultInputError.RESULT_NOT_FOUND, f"结果 {result_id} 不存在"
        )

    is_current = row.version_no == _latest_version_of(session, row.task_id)
    confirmation = (
        bool(row.manual_confirmed)
        and is_current
        and row.confirmed_digest == row.content_digest
    )

    try:
        focus_points = json.loads(row.focus_points_json) if row.focus_points_json else []
    except (TypeError, ValueError):  # pragma: no cover - 库里只应有合法 JSON
        focus_points = []

    return ResultView(
        result_id=row.id,
        run_id=row.run_id,
        task_id=row.task_id,
        version_no=row.version_no,
        is_current_version=is_current,
        confirmation_valid=confirmation,
        overall_risk_level=row.overall_risk_level,
        review_status=row.review_status,
        hit_count=row.hit_count,
        needs_review_count=row.needs_review_count,
        not_applicable_count=row.not_applicable_count,
        summary_text=row.summary_text or "",
        focus_points=list(focus_points),
        comment_text=row.comment_text or "",
        content_digest=row.content_digest or "",
        manual_confirmed=bool(row.manual_confirmed),
        confirmed_by=row.confirmed_by,
        confirmed_at=row.confirmed_at,
        confirmed_digest=row.confirmed_digest,
        supersedes_result_id=row.supersedes_result_id,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
