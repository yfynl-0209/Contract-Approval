"""审查结果、规则评价与**结果确认**接口（M7 / Task 3）。

## 为什么评价接口（`/api/evaluations`）与结果接口在同一个模块

它们是**同一批次的两种产出**：评价是"每条规则判了什么"，
结果是"这些评价聚合出的那份可回写的结论"。
分成两个模块时，"哪些评价属于这份结果"这件事就没有自然的归属 ——
而它恰恰是两个接口之间唯一的连接点（`run_id`）。
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app import tool_facade
from app.api.deps import get_db, require_permissions
from app.api.views import evaluation_json, iso, json_or_none, page_json
from app.auth import Actor, Permission
from app.enums import EvaluationStatus
from app.schemas import UpdateResultCommentRequest
from app.services import query_service
from app.services.rule_service import rule_names_by_code
from app.services.result_service import (
    ResultInputError,
    confirm_result,
    get_result_view,
)

router = APIRouter(prefix="/api", tags=["审查结果与人工确认（M7）"])


def result_row_json(session: Session, result_id: int) -> dict[str, Any]:
    """一份结果的对外形状 —— 列表与详情**共用**它。

    ⚠️ `confirmation_valid` 走的是 `confirmation_valid()` 的完整口径
    （已确认 + 摘要相符 + **仍是当前版本**），而不是"列个摘要让浏览器自己比"。
    前端自行比对时，改版后算错的方向恰恰是最危险的那一边
    （把失效的确认显示成有效），而它不会有任何报错。
    """
    view = get_result_view(session, result_id=result_id)

    return {
        "result_id": view.result_id,
        "run_id": view.run_id,
        "task_id": view.task_id,
        "version_no": view.version_no,
        "is_current_version": view.is_current_version,
        "confirmation_valid": view.confirmation_valid,
        "overall_risk_level": view.overall_risk_level,
        "review_status": view.review_status,
        "hit_count": view.hit_count,
        "needs_review_count": view.needs_review_count,
        "not_applicable_count": view.not_applicable_count,
        "summary_text": view.summary_text,
        "focus_points": view.focus_points,
        "comment_text": view.comment_text,
        # 下发的是**正文摘要**，不是正文本身：它是人工确认绑定的对象，
        # 调用方据此能回答"我确认的是不是这一份"，而它不构成机密。
        "content_digest": view.content_digest,
        "manual_confirmed": view.manual_confirmed,
        "confirmed_by": view.confirmed_by,
        "confirmed_at": iso(view.confirmed_at),
        "confirmed_digest": view.confirmed_digest,
        "supersedes_result_id": view.supersedes_result_id,
        "created_by": view.created_by,
        "created_at": iso(view.created_at),
        "updated_at": iso(view.updated_at),
        "status_url": f"/api/results/{view.result_id}",
    }


# ============================================================
# 结果列表
# ============================================================


@router.get(
    "/results",
    summary="审查结果列表（分页）",
    description=(
        "按 `created_at DESC, id DESC` 返回本租户的结果。\n\n"
        "每一行与 `GET /api/results/{id}` 的形状**完全一致** ——"
        "列表里带 `confirmation_valid`，因此界面不需要为每一行再发一次详情请求。\n\n"
        "`task_id` 可选：只列某条任务的结果（含它的全部历史版本，"
        "`is_current_version` 逐行标明哪个是当前版本）。"
    ),
)
def list_results(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(
        default=query_service.DEFAULT_PAGE_SIZE, ge=1, le=query_service.MAX_PAGE_SIZE
    ),
    task_id: int | None = Query(default=None, description="只列该任务的结果"),
    session: Session = Depends(get_db),
    actor: Actor = Depends(require_permissions(Permission.TASK_READ)),
) -> dict[str, Any]:
    result = query_service.list_results(
        session,
        tenant_id=actor.tenant_id,
        page=page,
        page_size=page_size,
        task_id=task_id,
    )
    return page_json(result, lambda row: result_row_json(session, row.id))


@router.get(
    "/results/{result_id}",
    summary="读取审查结果（含后端计算的确认有效性）",
    description=(
        "与列表行的形状一致。\n\n"
        "⚠️ `confirmation_valid` **由后端给出**，三个条件缺一不可："
        "已人工确认、确认时绑定的摘要与当前正文摘要相等、**且该版本仍是任务当前版本**。\n"
        "只查前两条会漏掉版本接替 —— 而那正是「旧版本的确认还有效」这个错觉的来源。\n\n"
        "跨租户与不存在都返回 **404**（不给枚举线索）。"
    ),
)
def get_result(
    result_id: int,
    session: Session = Depends(get_db),
    actor: Actor = Depends(require_permissions(Permission.TASK_READ)),
) -> dict[str, Any]:
    _assert_result_visible(session, result_id=result_id, tenant_id=actor.tenant_id)
    return result_row_json(session, result_id)


def _assert_result_visible(session: Session, *, result_id: int, tenant_id: str) -> None:
    """归属校验：结果所属任务必须属于本租户，否则 404。

    ⚠️ 判据挂在**任务**上而不是结果上：`review_results` 没有 `tenant_id`，
    它的归属由 `task_id` 传递而来。只查结果表存在性等于"知道 result_id
    就能读到别人租户的结论正文" —— 而 result_id 是连续的整数。

    ⚠️ "不存在"与"别人的"给**同一个** 404，消息也逐字相同：
    两者可区分时，状态码与消息就成了一个逐位试出 id 的探针。

    ⚠️ 错误码沿用 M6 就承诺出去的 `RESULT_NOT_FOUND`，而不是
    `RESOURCE_NOT_FOUND`：前者是**这个资源**的稳定机器码（调用方已经按它
    写好了分支），后者是通用引用错误。改码不在本任务的范围内 ——
    而"顺手统一一下"会让既有调用方在**运行到这一行时**才失效。
    """
    from app.models import ReviewResult

    row = session.get(ReviewResult, result_id)
    visible = row is not None and _task_belongs_to_tenant(
        session, task_id=row.task_id, tenant_id=tenant_id
    )
    if not visible:
        raise ResultInputError(
            ResultInputError.RESULT_NOT_FOUND, f"结果 {result_id} 不存在"
        )


def _task_belongs_to_tenant(session: Session, *, task_id: int, tenant_id: str) -> bool:
    """结果 → 任务 → 租户。走 `query_service` 的同一入口，避免每家自己 join。"""
    task = query_service.task_of(session, task_id)
    return task is not None and task.tenant_id == tenant_id


# ============================================================
# 结果确认
# ============================================================


@router.post(
    "/results/{result_id}/confirm",
    summary="人工确认审查结果与回写正文",
    description=(
        "把确认绑定到**后端计算的当前正文摘要**，留痕确认人，并写入不可变审计事件。\n\n"
        "⚠️ 调用方**没有传摘要的入口**：\"确认了哪份正文\"因此不是可以伪造的事实。\n\n"
        "**幂等**：同一人对同一版本确认两次是**一次**业务事实，"
        "不换时间、不换人、不追加审计事件。\n\n"
        "⚠️ 新版本结果一旦产生，旧版本上的确认**立即失效**"
        "（`confirmation_valid` 会变成 `false`）——"
        "确认绑定的是当时那份正文，而当前版本已经不是它。\n\n"
        "跨租户与不存在都返回 **404**。"
    ),
)
def confirm_result_route(
    result_id: int,
    session: Session = Depends(get_db),
    actor: Actor = Depends(require_permissions(Permission.RESULT_CONFIRM)),
) -> dict[str, Any]:
    _assert_result_visible(session, result_id=result_id, tenant_id=actor.tenant_id)
    confirm_result(session, result_id=result_id, actor=actor)
    return result_row_json(session, result_id)


# ============================================================
# 人工修改回写正文（M8 Task 7 的**薄出口**）
# ============================================================


@router.post(
    "/results/{result_id}/comment",
    summary="人工修改回写正文（生成新版本）",
    description=(
        "把回写正文改成 `comment_text`，并在 `review_results` 里生成**新版本**。\n\n"
        "## 它为什么存在：能力早就有，缺的是 `/api/` 出口\n\n"
        "控制台只调 `/api/*`（材料 §Global Constraints），而\"保存新版正文\""
        "此前只挂在 `/tools/save_review_result` 上 —— 于是控制台里的"
        "「编辑正文」没有任何可用的入口。\n\n"
        "⚠️ 本接口**不重新实现**版本化。它读当前结果、取它自己的 "
        "`run_id` / `overall_risk_level` / `summary_text` / `focus_points`，"
        "只把 `comment_text` 换成新正文，然后调用**工具 6 用的同一个服务函数**。"
        "新版本号、版本链（`supersedes_result_id`）、摘要重算、审计事件"
        "全部在那里发生 —— 两处各写一遍的分叉方式是\"某天只改了其中一处\"，"
        "而没有一处会报错。\n\n"
        "**返回形状与工具 6 逐字相同**（`outcome` / `result_id` / `run_id` / "
        "`task_id` / `version_no` / `overall_risk_level` / `content_digest` / "
        "`result_url`）。\n\n"
        "## 两件调用方必须知道的事\n\n"
        "1. **新版本一定未确认**：确认绑定的是**当时那份正文**，"
        "而正文已经变了。旧版本的 `confirmation_valid` 随之失效 ——"
        "这不是本接口\"撤销\"了确认，而是 `confirmation_valid` 的口径里"
        "本来就有\"仍是当前版本\"这一条（见 `result_service.get_result_view`）。"
        "要拿新的确认状态请再读一次 `result_url`。\n"
        "2. **同一份正文保存两次不会多出版本**：命中文档指纹时返回 "
        "`outcome=\"reused\"` 且复用既有版本。\n\n"
        "**只允许改正文**：风险等级 / 摘要 / 关注点不由这里修改 ——"
        "工具 6 要求 `overall_risk_level` 与批次聚合一致，"
        "把这个选择重新开放给控制台等于重开一个已被 `RESULT_INPUT_MISMATCH` "
        "关掉的口子。\n\n"
        "**权限**：`result:save`（与工具 6 同一项），不是 `result:confirm` ——"
        "改正文与确认结论是两件事，权限上有意分开。\n\n"
        "跨租户与不存在都返回 **404**（不给枚举线索）。"
    ),
)
def update_result_comment(
    result_id: int,
    payload: UpdateResultCommentRequest,
    session: Session = Depends(get_db),
    actor: Actor = Depends(require_permissions(Permission.RESULT_SAVE)),
) -> dict[str, Any]:
    _assert_result_visible(session, result_id=result_id, tenant_id=actor.tenant_id)

    # ⚠️ 取**当前结果自己**的口径，而不是重新聚合：这些值在该结果保存时
    # 已经被 `save_review_result` 校验过一次（等级必须等于批次聚合）。
    # 重新聚合反而会让"编辑正文"顺带把风险等级改成另一版聚合的结果 ——
    # 一次改正文的操作不该产生一个结论不同的版本。
    current = get_result_view(session, result_id=result_id)

    return tool_facade.save_review_result(
        str(current.run_id),
        current.overall_risk_level,
        current.summary_text,
        json.dumps(list(current.focus_points), ensure_ascii=False),
        payload.comment_text,
        session=session,
        actor=actor,
    )


# ============================================================
# 规则评价
# ============================================================


@router.get(
    "/evaluations",
    summary="规则评价列表（四态，需关注的排前）",
    description=(
        "返回本租户的规则评价，**四态都给**（`hit` / `not_hit` / "
        "`not_applicable` / `needs_review`）。\n\n"
        "**默认排序把 `hit` 与 `needs_review` 排在前面**，但一条都不删除 ——\n"
        "只返回 `hit` 时，\"这条规则为什么没报警\"就永远答不出来，"
        "而那正是四态记录存在的理由。\n\n"
        "同一权限内按 `rule_code` 稳定排序，最后以 `id` 兜底："
        "同一批次的评价是**一次事务里批量写入**的，`created_at` 极可能完全相同，"
        "只按时间排序时翻页会重复或漏行。\n\n"
        "`run_id` 可选：只列某一次批次的评价。"
    ),
)
def list_evaluations(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(
        default=query_service.DEFAULT_PAGE_SIZE, ge=1, le=query_service.MAX_PAGE_SIZE
    ),
    run_id: int | None = Query(default=None, description="只列该批次的评价"),
    evaluation_status: str | None = Query(
        default=None, description="hit/not_hit/not_applicable/needs_review"
    ),
    session: Session = Depends(get_db),
    actor: Actor = Depends(require_permissions(Permission.TASK_READ)),
) -> dict[str, Any]:
    result = query_service.list_evaluations(
        session,
        tenant_id=actor.tenant_id,
        page=page,
        page_size=page_size,
        run_id=run_id,
        evaluation_status=evaluation_status,
    )
    # 与 `/api/runs/{id}` 同一纪律：编码给机器，名字给人 —— 查表补齐
    rule_names = rule_names_by_code(session, [rule_code for _, rule_code in result.items])

    def serialize(row: Any) -> dict[str, Any]:
        evaluation, rule_code = row
        return {
            "evaluation_id": evaluation.id,
            "run_id": evaluation.run_id,
            "task_id": evaluation.task_id,
            "rule_version": evaluation.rule_version,
            **evaluation_json(
                rule_code=rule_code,
                rule_name=rule_names.get(rule_code),
                evaluation_status=evaluation.evaluation_status,
                risk_level=evaluation.risk_level,
                reason_code=evaluation.reason_code,
                reason_text=evaluation.reason_text,
                evidence_json=evaluation.evidence_json,
                hit_detail=json_or_none(evaluation.hit_detail_json),
            ),
            "created_at": iso(evaluation.created_at),
        }

    return page_json(result, serialize)


#: 便于调用方构造过滤参数（取值域的唯一定义在 `app/enums.py`）。
EVALUATION_STATUS_VALUES: tuple[str, ...] = tuple(
    item.value for item in EvaluationStatus
)
