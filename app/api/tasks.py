"""任务查询与**立场确认**接口（M7 / Task 3）。

## 本模块与 `app/api/tools.py` 的分工

工具接口是**给外部系统集成**用的（需求 2.4.10 的七个名字与签名逐字不变）；
本模块是**给自己人看的控制台**用的（列表、详情、人工确认）。

两者的响应体**刻意不同**：工具接口的字段由需求的契约决定，
控制台接口的字段由"人需要看到什么"决定。强行合并会让其中一边
要么缺字段、要么承诺了一堆与它无关的兼容名。

## 为什么列表与详情共用同一个 `task_view`

分成两套时，"列表里的状态"与"详情里的状态"迟早会漂移 ——
漂移的方式是某天给详情加了一个字段、忘了列表，
而使用者看到的是同一个任务在两张页面上显示**不同的状态**。

## 立场确认的权限为什么是 `result:confirm`

M7 定的 8 项权限里没有 `context:confirm`。新增第 9 项会改动
需求 §6 的权限清单（那是一个对外承诺），而"人工确认"这一档能力
（法务审核人有、只读审计没有）**恰好**是 `result:confirm` 覆盖的。
若日后需求把两者分开，这里改成新权限是一行的事 ——
而**现在**先合并的理由是：立场确认如果没有任何权限挡着，
只读审计就能改我方立场，而那个字段是回写门禁的输入。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_permissions
from app.api.views import iso, json_or_none, page_json
from app.errors import BUSINESS_FACT_CODES
from app.auth import Actor, Permission
from app.schemas import ConfirmContextRequest
from app.enums import TaskStatus
from app.services import query_service

router = APIRouter(prefix="/api", tags=["任务查询与人工确认（M7）"])


# ============================================================
# 任务列表 / 详情
# ============================================================


@router.get(
    "/tasks",
    summary="任务列表（分页）",
    description=(
        "按 `created_at DESC, id DESC` 返回本租户的任务。\n\n"
        "**总数为满足条件的行数**，不是当前页长度。\n\n"
        "`task_status` 是**白名单枚举**：拼错的值得到 400 而不是空列表 ——"
        "空列表会被读成\"没有阻塞的任务\"，而真相是\"这个过滤值从来不存在\"。"
    ),
)
def list_tasks(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(
        default=query_service.DEFAULT_PAGE_SIZE, ge=1, le=query_service.MAX_PAGE_SIZE
    ),
    task_status: str | None = Query(default=None, description="pending/parsing/reviewing/blocked/done"),
    session: Session = Depends(get_db),
    actor: Actor = Depends(require_permissions(Permission.TASK_READ)),
) -> dict[str, Any]:
    result = query_service.list_tasks(
        session,
        tenant_id=actor.tenant_id,
        page=page,
        page_size=page_size,
        task_status=task_status,
    )
    return page_json(result, _task_row_serializer(session, result))


def _task_row_serializer(
    session: Session, page: query_service.Page
) -> Callable[[Any], dict[str, Any]]:
    """列表行的序列化：`task_view` + 总风险等级 + **回写的两个层级**。

    ⚠️ 风险等级与最近一次回写尝试都**一次批量取回**（`current_results` /
    `latest_writeback_attempts`），不是逐行查：逐行查是 N+1，
    而它错得没有症状 —— 页面上只是慢一点。

    ⚠️ 没有结果时给 `null`，**不是** `low`：把"没审过"显示成"低风险"
    是最危险的一种默认值（设计 §4.1）。

    ⚠️ 回写带上 `writeback` 对象（与详情**同名同结构**）而不是只给任务级状态：
    设计 §5.1 要求"状态与原因分两处呈现"，而列表是用户第一眼看到的地方 ——
    只显示 `写失败` 会让人去重试，而"被门禁拒绝"的重试**永远是白试**。
    """
    task_ids = [task.id for task in page.items]
    levels = query_service.current_results(session, task_ids)
    attempts = query_service.latest_writeback_attempts(session, task_ids)
    attachments = query_service.attachment_counts(session, task_ids)

    def serialize(task: Any) -> dict[str, Any]:
        current = levels.get(task.id)
        return {
            **query_service.task_view(task),
            "overall_risk_level": (
                None if current is None else current.overall_risk_level
            ),
            "writeback": _writeback_json(
                query_service.writeback_summary_of(task, attempts.get(task.id))
            ),
            "attachment_count": attachments.get(task.id, 0),
        }

    return serialize


@router.get(
    "/tasks/summary",
    summary="任务汇总计数（服务端聚合）",
    description=(
        "本租户的任务总数、按状态分组的计数，以及回写失败数。\n\n"
        "⚠️ **必须由服务端算**：列表卡片说的是「全量」，而前端只能数到当前这一页 —— "
        "数据超过一页时两者不等，而在数据少时**永远相等**，因此这个缺陷会一直活到上线之后。\n\n"
        "`by_status` 的键**恒定齐全**（五个状态即使为 0 也在）：缺键时前端会渲染出 "
        "`undefined`，而「0」与「没有这个键」在界面上分不开。\n\n"
        "⚠️ **路由必须注册在 `/tasks/{task_id}` 之前**：`{task_id}` 是 `int`，"
        "对 `summary` 会得到 422 —— 而如果反过来先注册了通配路由，"
        "这个接口会**静默地永远不生效**（请求被前一条吃掉），"
        "`tests/test_task_queries.py` 有一条用例守着它。"
    ),
)
def get_task_summary(
    session: Session = Depends(get_db),
    actor: Actor = Depends(require_permissions(Permission.TASK_READ)),
) -> dict[str, Any]:
    summary = query_service.task_summary(session, tenant_id=actor.tenant_id)
    return {
        "total": summary.total,
        "by_status": dict(summary.by_status),
        "writeback_failed": summary.writeback_failed,
    }


@router.get(
    "/tasks/{task_id}",
    summary="任务详情",
    description=(
        "任务本身的状态 + **链路指针**（最新解析 / 最新批次 / 当前结果 / 附件数）\n"
        "+ **两个层级的回写口径**。\n\n"
        "| 层级 | 字段 | 回答 |\n"
        "| --- | --- | --- |\n"
        "| 任务级 | `writeback.task_write_status` | 这张单子写成了没有 |\n"
        "| 尝试级 | `writeback.latest_*` | **最近这一次**为什么没成 |\n\n"
        "两者**不可互相替代**：任务级说 `failed` 时只知道\"没写成\"，\n"
        "而一条 `not_written` + `MANUAL_CONFIRM_REQUIRED` 才说明\"它在等一次人工确认\" ——\n"
        "重试一个被拒的请求永远是白试。\n\n"
        "⚠️ **跨租户与不存在都返回 404**，且都是 `RESOURCE_NOT_FOUND`：\n"
        "403 会确认\"这个 id 存在\"，那是一个可以逐位试出别人 id 的枚举预言机。"
    ),
)
def get_task(
    task_id: int,
    session: Session = Depends(get_db),
    actor: Actor = Depends(require_permissions(Permission.TASK_READ)),
) -> dict[str, Any]:
    task = query_service.get_task(session, tenant_id=actor.tenant_id, task_id=task_id)
    chain = query_service.task_chain(session, task)
    summary = query_service.writeback_summary(session, task)

    current = query_service.current_results(session, [task.id]).get(task.id)

    return {
        **query_service.task_view(task),
        # 审批表单（原样下发键值对）。**掩码是呈现层的事**：
        # 服务端把原文给它授权的调用方，由界面默认遮住证件号/联系方式
        # （§4.2）。⚠️ 前端不得把它写进 URL、console 或错误上报。
        "form_data": json_or_none(task.form_data_json),
        # 「这是业务结论还是系统故障」在服务端算（§5.2）：
        # 判据是 `app/errors.py::BUSINESS_FACT_CODES` 那一份，
        # 前端自己列一张码表时会与它漂移，而漂移的后果是
        # "附件被删了"被渲染成"系统故障，稍后重试"。
        "last_error_is_business_fact": (
            task.last_error_code is not None
            and task.last_error_code in {code.value for code in BUSINESS_FACT_CODES}
        ),
        # 与列表行**同名同义**（详情与列表的形状刻意一致）：
        # 两套时，"列表里的风险"与"详情里的风险"迟早漂移。
        "overall_risk_level": None if current is None else current.overall_risk_level,
        "latest_parse_id": chain.latest_parse_id,
        "latest_run_id": chain.latest_run_id,
        "current_result_id": chain.current_result_id,
        "attachment_count": chain.attachment_count,
        # 关联 ID 取自最近一次**作业**：它是唯一同时覆盖"入队过"与
        # "Worker 执行过"两类事件的表，因此拿它去查日志总能查到东西。
        "correlation_id": chain.correlation_id,
        "writeback": _writeback_json(summary),
    }


def _writeback_json(summary: query_service.WritebackSummary) -> dict[str, Any]:
    return {
        "task_write_status": summary.task_write_status,
        "latest_attempt_id": summary.latest_attempt_id,
        "latest_attempt_no": summary.latest_attempt_no,
        "latest_attempt_status": summary.latest_attempt_status,
        "latest_reason_code": summary.latest_reason_code,
        "latest_reason_text": summary.latest_reason_text,
        "latest_attempt_at": iso(summary.latest_attempt_at),
        # 单独给一个布尔量，是因为它决定的**处置方向**与"失败"相反：
        # 拒绝要人去确认，失败要人去重试。调用方从 reason_code 推也行，
        # 但那意味着每个调用方都要知道哪些原因是"拒绝类"的。
        "latest_attempt_rejected": summary.latest_attempt_rejected,
        "status_url": (
            None
            if summary.latest_attempt_id is None
            else f"/api/writebacks/{summary.latest_attempt_id}"
        ),
    }


# ============================================================
# 立场确认（Context Confirmation）
# ============================================================


@router.post(
    "/tasks/{task_id}/context/confirm",
    summary="人工确认权威审查上下文（我方立场）",
    description=(
        "把任务的 `context_status` 置为 `confirmed` 并写入不可变审计事件。\n\n"
        "## 请求体**可选**，它决定这是「确认」还是「修正」\n\n"
        "| 请求体 | 允许的状态 | 效果 |\n"
        "| --- | --- | --- |\n"
        "| 不带 | **只有** `complete` | 确认审批系统给出的立场 |\n"
        "| 带（四条齐全） | 任意，含 `missing` / `conflict` | **人工给出**这四条业务事实并同时确认 |\n\n"
        "⚠️ `missing` / `conflict` 下**必须**带请求体：它们没有可确认的对象。 "
        "而 `missing` 是「刚拉取完任务」的正常状态 —— "
        "没有修正这条路时，这类任务会永久停住（回写门禁要求可信立场）。\n"
        "`conflict` 下带请求体即「人工裁定」。\n\n"
        "⚠️ 修正必须**四条齐全**：只给其中两条时剩下两条还是旧值，"
        "于是「我方是谁」与「这对我是好是坏」可能自相矛盾，"
        "而这类不一致不会被任何校验发现，只会让规则方向判错。\n\n"
        "⚠️ 与 `POST /api/results/{id}/confirm` **不是同一件事**：\n"
        "本接口确认的是**审查立场**（我方 / 合同标签 / 业务角色 / 合同类型），\n"
        "那个接口确认的是**审查结果与回写正文**。\n"
        "立场对了不代表结果认可，反之亦然 —— 合并成一个动作时，"
        "\"我只想确认立场，结果还要再看看\"就做不到了。\n\n"
        "**不带请求体时只有 `complete` 可确认**：`missing` 表示我方立场还没拿到、\n"
        "`conflict` 表示两个来源互相矛盾，两者都**没有可确认的对象** ——\n"
        "强行放行等于让人确认一个我们说不清是什么的东西，"
        "而回写门禁随后会把它当成可信立场使用。\n"
        "**带请求体时任意状态都可以**：这次是人**给出**立场，而不是认可它。\n\n"
        "## `conflict` 是怎么产生的\n\n"
        "**人工确认过的立场**与审批系统后续同步回来的声明不一致时，"
        "同步侧把状态置为 `conflict` 并保留人工那一组值"
        "（`context_conflict` 给出两个来源的对照）。\n"
        "此时回写门禁**拒绝**（`conflict` 不在可信集合里）—— 这是刻意的：\n"
        "用一份被新数据否决的立场继续回写，会把规则方向判反而报告上看不出异常。\n\n"
        "**幂等**：重复确认不换时间、不换人、不追加审计事件。"
    ),
)
def confirm_context(
    task_id: int,
    payload: ConfirmContextRequest | None = None,
    session: Session = Depends(get_db),
    actor: Actor = Depends(require_permissions(Permission.RESULT_CONFIRM)),
) -> dict[str, Any]:
    # ⚠️ 请求体**可选**：不带体 = 确认既有立场（只有 `complete` 能确认）；
    # 带体 = 人工**修正**四条业务事实并同时确认（`missing` / `conflict` 下
    # 这是唯一的出路，见 `query_service.confirm_context` 的说明）。
    task = query_service.confirm_context(
        session,
        tenant_id=actor.tenant_id,
        task_id=task_id,
        actor=actor,
        correction=None if payload is None else payload.model_dump(mode="json"),
    )
    return {
        "task_id": task.id,
        "context_status": task.context_status,
        "context_source": task.context_source,
        "our_party_name": task.our_party_name,
        "our_party_contract_label": task.our_party_contract_label,
        "our_party_business_role": task.our_party_business_role,
        "contract_type": task.contract_type,
        "status_url": f"/api/tasks/{task.id}",
    }


#: 便于调用方构造过滤参数（取值域的唯一定义在 `app/enums.py`）。
TASK_STATUS_VALUES: tuple[str, ...] = tuple(item.value for item in TaskStatus)
