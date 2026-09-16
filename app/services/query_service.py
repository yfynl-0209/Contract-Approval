"""任务 / 作业 / 规则评价 / 审查结果的**只读查询**（M7 / Task 3）。

## 为什么分页、排序与总数落在这层

接口层不得出现业务分支（README §4.1）。而这一层里每一件事都是**业务口径**：

| 决定 | 谁定的 | 定错的后果 |
| --- | --- | --- |
| 总数怎么算 | 这里 | 前端拿"当前页长度"当总数 → 永远只有一页 |
| 排序稳不稳 | 这里 | 同一时刻的两行在两次请求里换位 → 分页**重复或漏掉**记录 |
| 跨租户给 403 还是 404 | 这里 | 403 等于确认"这个 id 存在"，是一个**枚举预言机** |
| 四态评价给不给全 | 这里 | 只给 `hit` 时"这条规则为什么没报警"永远答不出来 |

## 租户可见性：**每一次**查询都要过，含嵌套资源

`actor.tenant_id` 是身份提供方说的话，`approval_tasks.tenant_id` 是任务归属。
两者不等就是**看不见**，而不是"看得见但不许改"。

⚠️ **跨租户直接猜 id 一律 404，与"不存在"不可区分。** 这是刻意的：
用 403 时，攻击者能拿状态码当探针逐位试出别人的 task_id ——
"403 说明存在、404 说明不存在"，一次遍历就拿到全量 id 与它们的数量。
改 id 猜不猜得中与**数据有没有泄漏**是两件事，而 404 让后者不成立。

## 嵌套 id 也要过同一道门

`/api/jobs/{id}` 指向的作业属于某条任务，而任务属于某个租户。
只查作业表会漏掉归属判断 —— 于是"知道 job_id 就能看到别人租户的作业状态、
失败原因、关联的 `parse_id`"。这些字段单独看都不敏感，
但它们是**逐层拼出对方数据结构的碎片**。因此本模块里所有嵌套查询
都先 join 回 `approval_tasks` 再过滤租户。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Select, case, func, or_, select
from sqlalchemy.orm import Session

from app.enums import (
    AuditAction,
    ContextSource,
    ContextStatus,
    ErrorCode,
    EvaluationStatus,
    JobStatus,
    LogLevel,
    LogType,
    TaskStatus,
    WriteStatus,
)
from app.errors import PermanentError
from app.models import (
    ApprovalAttachment,
    ApprovalTask,
    AuditEvent,
    CommentLog,
    ContractParse,
    ReviewResult,
    ReviewRule,
    ReviewRun,
    RuleEvaluation,
    TaskLog,
    WorkflowJob,
)

#: 默认每页条数。与需求里"待办拉取默认 20"保持一致，减少一个要记的数字。
DEFAULT_PAGE_SIZE = 20

#: 每页上限。**必须有限制**：不限时 `page_size=100000` 会把一次列表查询
#: 变成一次全表扫描 + 全量序列化，而请求看起来完全正常。
MAX_PAGE_SIZE = 100


# ============================================================
# 分页
# ============================================================


@dataclass(frozen=True, slots=True)
class Page:
    """一页数据 + **由后端算出的总数**。

    ⚠️ `total` 是**满足条件的总行数**，不是 `len(items)`。
    两者在"只有一页"时永远相等 —— 那正是这个缺陷能活到生产的原因：
    前端的页数计算在数据量小的时候看起来完全正常。
    """

    items: Sequence[Any]
    total: int
    page: int
    page_size: int

    @property
    def page_count(self) -> int:
        """总页数。空结果集也是 1 页（"第 1 页，共 0 条"比"共 0 页"好读）。"""
        if self.total == 0:
            return 1
        return (self.total + self.page_size - 1) // self.page_size

    @property
    def has_next(self) -> bool:
        return self.page < self.page_count


def _validate_paging(page: int, page_size: int) -> tuple[int, int]:
    """校验分页参数，非法时抛 `ValueError`（→ 400，本仓库对调用方参数错误的约定）。

    ⚠️ **不静默钳制**。把 `page=0` 悄悄改成 1、把 `page_size=9999` 悄悄压到 100，
    会让调用方以为"我拿到的就是第 0 页/一万条"，而分页游标在他手里已经错了 ——
    表现为"翻页时数据对不上"，且没有任何提示。
    """
    if page < 1:
        raise ValueError(f"page 必须 >= 1，实际收到 {page}")
    if page_size < 1 or page_size > MAX_PAGE_SIZE:
        raise ValueError(f"page_size 必须在 1..{MAX_PAGE_SIZE} 之间，实际收到 {page_size}")
    return page, page_size


def check_member(
    value: str | None, allowed: frozenset[str], *, parameter: str
) -> str | None:
    """枚举型过滤参数的白名单校验。

    ⚠️ **不校验时不会报错，只会静默返回空集**：`?task_status=blockd`（拼错）
    得到一个空列表，调用方会读成"没有阻塞的任务" —— 而真相是"这个过滤值
    从来不存在"。空集是这两种情况的**唯一**表现，因此必须在这里断开。

    公开（而不是 `_` 前缀）是因为**规则列表**等本模块之外的查询也需要
    同一份判据；各处自己写一遍时，写法差异（`in` 还是 `==`、
    报错消息里放不放允许值）会让同一个拼写错误在不同接口上有不同表现。
    """
    if value is None:
        return None
    if value not in allowed:
        raise ValueError(
            f"{parameter} 只能是 {sorted(allowed)} 之一，实际收到 {value!r}"
        )
    return value


def _fetch_page(
    session: Session, stmt: Select, *, page: int, page_size: int, scalars: bool
) -> tuple[list[Any], int]:
    """取一页 + 总数。**总数走单独的 `COUNT(*)`**，不数这一页的行数。

    `COUNT(*)` 用的子查询与取行用的是**同一条 `stmt`**（去掉排序与分页），
    因此"总数"与"这些行"永远是同一个条件的结果 —— 分成两条 SQL 时，
    两者之间的写入会让它们说的不是同一件事。

    ⚠️ `scalars` 必须由调用方**显式**指定，而不是用"只选了一列就取标量"
    去推断：`select(Entity)` 与 `select(Entity, other)` 在 SQLAlchemy 里
    都只产生一列/两列，而前者要的是**实体对象**、后者要的是**元组**。
    推断错了的表现是 `row.id` 抛 `KeyError`，深在序列化里。
    """
    total = session.execute(
        select(func.count()).select_from(stmt.order_by(None).subquery())
    ).scalar_one()

    result = session.execute(stmt.limit(page_size).offset((page - 1) * page_size))
    rows = list(result.scalars().all()) if scalars else list(result.all())
    return rows, int(total)


def _page(
    session: Session, stmt: Select, *, page: int, page_size: int, scalars: bool = True
) -> Page:
    page, page_size = _validate_paging(page, page_size)
    rows, total = _fetch_page(
        session, stmt, page=page, page_size=page_size, scalars=scalars
    )
    return Page(items=rows, total=total, page=page, page_size=page_size)


def paginate(
    session: Session, stmt: Select, *, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE,
    scalars: bool = True,
) -> Page:
    """公开的分页入口：让**本模块之外**的查询复用同一套分页口径。

    ⚠️ "总数走单独的 COUNT、与取行共用同一条 stmt"这件事只有一份实现。
    各处自己写一遍时，分叉方式是"某处忘了去掉 order_by"或
    "某处直接数了这一页的行数" —— 而两者在数据少于一页时**永远相等**，
    缺陷会一直活到数据量上来。
    """
    return _page(session, stmt, page=page, page_size=page_size, scalars=scalars)


# ============================================================
# 任务
# ============================================================


def _task_scope(tenant_id: str) -> Any:
    """租户可见性的**唯一**判据。所有查询都从它出发。"""
    return ApprovalTask.tenant_id == tenant_id


def list_tasks(
    session: Session,
    *,
    tenant_id: str,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    task_status: str | None = None,
) -> Page:
    """任务列表。

    排序：`created_at DESC, id DESC`。

    ⚠️ **`id` 是排序的一部分，不是装饰。** `created_at` 的精度只到秒
    （SQLite 的 `CURRENT_TIMESTAMP`），同一秒里创建的两条任务在两次请求之间
    可能换位 —— 翻到第 2 页时会**再看到一次**第 1 页的某条，同时**漏掉**另一条。
    加上唯一的 `id` 之后顺序才是全序的。
    """
    stmt = select(ApprovalTask).where(_task_scope(tenant_id))

    task_status = check_member(task_status, TASK_STATUSES, parameter="task_status")
    if task_status is not None:
        stmt = stmt.where(ApprovalTask.task_status == task_status)

    stmt = stmt.order_by(ApprovalTask.created_at.desc(), ApprovalTask.id.desc())
    return _page(session, stmt, page=page, page_size=page_size)


@dataclass(frozen=True, slots=True)
class TaskSummary:
    """任务汇总计数（**服务端聚合**，M8 缺口 G-M8-1）。

    ## 为什么必须有这个接口，而不是让前端数

    列表页的卡片要显示"待办 12 · 审查中 3 · 需人工 2 · 已完成 45 · 回写失败 1"。

    前端**只能**数到"当前这一页"（默认 20 行），而卡片说的是**全量**。
    数据一超过一页，两者就不相等 —— 而它们在数据少时**永远相等**，
    所以这个缺陷会一直活到上线之后。让前端自己再拉全量来数，
    则是第二个问题：那是**另一次请求**，与列表不是同一个快照，
    两次之间入库的任务会让卡片与列表自相矛盾。

    ⚠️ `by_status` 的键**恒定齐全**（五个状态即使为 0 也在）：
    缺键时前端渲染出 `undefined`，而"0"与"没有这个键"在界面上分不开。
    """

    total: int
    by_status: Mapping[str, int]
    writeback_failed: int


def task_summary(session: Session, *, tenant_id: str) -> TaskSummary:
    """本租户的任务计数：按状态分组 + 回写失败数。**两条 SQL，不是每个状态一次。**"""
    rows = session.execute(
        select(ApprovalTask.task_status, func.count())
        .where(_task_scope(tenant_id))
        .group_by(ApprovalTask.task_status)
    ).all()

    # 先用已知取值铺满零，再把库里数出来的填进去：
    # 反过来做（只放查到的）会让"某个状态一条都没有"变成缺键。
    by_status: dict[str, int] = {status: 0 for status in TASK_STATUSES}
    total = 0
    for status, count in rows:
        total += int(count)
        # 库里出现未知取值时**照样计入**（不静默丢行）：丢掉的话
        # `total` 与各状态之和对不上，而那种不一致没人查得出来。
        by_status[str(status)] = int(count)

    writeback_failed = int(
        session.execute(
            select(func.count())
            .select_from(ApprovalTask)
            .where(
                _task_scope(tenant_id),
                ApprovalTask.write_status == WriteStatus.FAILED.value,
            )
        ).scalar_one()
    )

    return TaskSummary(
        total=total, by_status=by_status, writeback_failed=writeback_failed
    )


# ============================================================
# 任务的**当前结果**（列表与详情共用一份判据）
# ============================================================


@dataclass(frozen=True, slots=True)
class CurrentResult:
    """某任务当前版本结果的最小信息（够列表页显示"总风险"用）。"""

    result_id: int
    overall_risk_level: str


def attachment_counts(
    session: Session, task_ids: Sequence[int]
) -> dict[int, int]:
    """一次取回**一批任务**各自的附件数（列表页的"附件"列用它）。

    ⚠️ 缺键与"0 个附件"是两件事：这里**只返回有附件的任务**，
    调用方用 `.get(task_id, 0)` 明确表达"没有就是 0"。反过来（把所有任务
    都填成 0）会让"这个 id 不存在"与"没有附件"分不开 —— 而前者意味着
    调用方传错了 id，那是需要暴露的错误。
    """
    ids = list(dict.fromkeys(int(task_id) for task_id in task_ids))
    if not ids:
        return {}

    rows = session.execute(
        select(ApprovalAttachment.task_id, func.count())
        .where(ApprovalAttachment.task_id.in_(ids))
        .group_by(ApprovalAttachment.task_id)
    ).all()
    return {int(task_id): int(count) for task_id, count in rows}


def current_results(
    session: Session, task_ids: Sequence[int]
) -> dict[int, CurrentResult]:
    """一次取回**一批任务**各自的当前结果（M8 缺口 G-M8-3）。

    ## 为什么是"批量函数"而不是逐个任务查

    列表页每行都要显示"总风险"。逐行查是 N+1（默认 20 行 = 20 条 SQL），
    而它错得**没有症状**：页面上只是慢一点。用一条 `row_number()` 窗口查询
    取回整页，成本与页大小无关。

    ## 为什么判据必须与 `task_chain` 一致

    取"当前"的规则是 `version_no DESC, id DESC`。列表与详情各写一遍时，
    分叉方式是某天只改了其中一处 —— 于是**同一条任务在列表与详情上显示不同的风险等级**，
    而两处都"看起来对"。因此这里只留一份实现，`task_chain` 也改用它。

    返回的字典只包含**有结果**的任务；没有结果的任务**不在键里**
    （调用方必须显式处理 `None`，不能靠 `.get(id)` 拿到一个默认值就渲染 ——
    把"没审过"显示成"低风险"是最危险的一种默认值）。
    """
    # 去重并保持顺序：同一页里 task_id 不会重复，但调用方可能传重（详情只传一个）。
    ids = list(dict.fromkeys(int(task_id) for task_id in task_ids))
    if not ids:
        # ⚠️ `IN ()` 在部分方言里是语法错误，在另一些里是"匹配一切"。
        # 空输入直接返回，不把这件事交给数据库去理解。
        return {}

    ranked = (
        select(
            ReviewResult.task_id.label("task_id"),
            ReviewResult.id.label("result_id"),
            ReviewResult.overall_risk_level.label("overall_risk_level"),
            func.row_number()
            .over(
                partition_by=ReviewResult.task_id,
                order_by=(ReviewResult.version_no.desc(), ReviewResult.id.desc()),
            )
            .label("rank"),
        )
        .where(ReviewResult.task_id.in_(ids))
        .subquery()
    )

    rows = session.execute(select(ranked).where(ranked.c.rank == 1)).all()
    return {
        int(row.task_id): CurrentResult(
            result_id=int(row.result_id),
            overall_risk_level=str(row.overall_risk_level),
        )
        for row in rows
    }


def list_parses(
    session: Session,
    *,
    tenant_id: str,
    task_id: int,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> Page:
    """某任务的**解析版本列表**（新版本在前，M8 缺口 G-M8-4 的剩余部分）。

    ## 为什么按 `parse_version DESC, id DESC` 排

    界面上的默认选择是"最新那次解析"，而**版本切换**（验收 12：切换版本时
    字段与 PDF 必须同时切换）靠的就是这份列表。排序不稳定时，
    同一份列表两次请求可能给出不同的第一行 —— 于是"默认选中哪个版本"
    会随机变化，而字段与 PDF 各自取的版本可能**不是同一个**。

    ## `parse_version` 与 `parser_version` 不是一回事

    前者是"第几次解析"，后者是"哪个解析器版本产生的"。界面要显示的是前者；
    两者混用会让解析器升级后的旧记录仍显示成新版本。

    ⚠️ 归属判断走**任务**（`contract_parses` 没有 `tenant_id`，它的归属由
    `task_id` 传递而来）—— 只查解析表等于"知道 task_id 就能看到别人租户的解析版本"。
    """
    stmt = (
        select(ContractParse)
        .where(
            ContractParse.task_id == task_id,
            ContractParse.task_id.in_(
                select(ApprovalTask.id).where(_task_scope(tenant_id))
            ),
        )
        .order_by(ContractParse.parse_version.desc(), ContractParse.id.desc())
    )
    return _page(session, stmt, page=page, page_size=page_size)


def list_attachments(
    session: Session,
    *,
    tenant_id: str,
    task_id: int,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> Page:
    """某任务的**附件列表**（M8 模块 2）。

    按 `id ASC` 排：附件没有天然的"新在前"语义，而**顺序稳定**才是界面需要的
    （列表两次请求顺序不同时，用户刚点的那一行会跑到别处）。

    ⚠️ 归属判断走**任务**（`approval_attachments` 的归属由 `task_id` 传递而来）。
    ⚠️ 这里**只读库里的元数据**，不碰字节 —— 字节一律经
    `GET /api/attachments/{id}/content` 流出（见 `app/api/attachments.py`）。
    """
    stmt = (
        select(ApprovalAttachment)
        .where(
            ApprovalAttachment.task_id == task_id,
            ApprovalAttachment.task_id.in_(
                select(ApprovalTask.id).where(_task_scope(tenant_id))
            ),
        )
        .order_by(ApprovalAttachment.id.asc())
    )
    return _page(session, stmt, page=page, page_size=page_size)


def get_task(session: Session, *, tenant_id: str, task_id: int) -> ApprovalTask:
    """按 id 取任务；跨租户与不存在**都**抛 404。

    Raises:
        PermanentError: `RESOURCE_NOT_FOUND` —— 与"这个 id 不存在"不可区分。
    """
    task = session.execute(
        select(ApprovalTask).where(
            ApprovalTask.id == task_id, _task_scope(tenant_id)
        )
    ).scalar_one_or_none()

    if task is None:
        # 消息里**不带** tenant_id：带上去等于用错误信息确认"这个 id 存在，
        # 只是不属于你" —— 那正是 404 想避免的枚举线索。
        raise PermanentError(
            f"任务 {task_id} 不存在", code=ErrorCode.RESOURCE_NOT_FOUND
        )
    return task


def task_view(task: ApprovalTask) -> dict[str, Any]:
    """任务的**列表行**形状（不含派生链路的细节）。

    ⚠️ **不收 `session`**：它只读任务自己的列。收一个用不到的
    `session` 会让调用方以为"这里还会查别的东西"，
    而分页信封（`views.page_json`）也才能直接把它当成逐行序列化函数用。

    详情与列表共用它，两者的区别只在详情多挂几个字段 ——
    分成两套时，"列表里的状态"与"详情里的状态"迟早会漂移。
    """
    return {
        "task_id": task.id,
        "instance_id": task.instance_id,
        "approval_code": task.approval_code,
        "approval_title": task.approval_title,
        "applicant_name": task.applicant_name,
        "task_status": task.task_status,
        "write_status": task.write_status,
        "context_status": task.context_status,
        "context_source": task.context_source,
        # 冲突时给出**两个来源的对照**（`{declared, confirmed}`），否则为 `None`。
        # ⚠️ 与 `context_status` 一起下发，而不是让界面自己去猜哪两个值不一致 ——
        # 那需要它知道"库里这份是人工改的还是系统给的"，而那是历史信息。
        "context_conflict": _json_or_none(task.context_conflict_json),
        "our_party_name": task.our_party_name,
        "our_party_contract_label": task.our_party_contract_label,
        "our_party_business_role": task.our_party_business_role,
        "contract_type": task.contract_type,
        "blocked_stage": task.blocked_stage,
        "block_reason": task.block_reason,
        "last_error_code": task.last_error_code,
        "retry_count": task.retry_count,
        "created_at": _iso(task.created_at),
        "updated_at": _iso(task.updated_at),
        "status_url": f"/api/tasks/{task.id}",
    }


# ============================================================
# 任务详情：任务级与尝试级的**两个**回写口径（M7 关闭缺口 G-4）
# ============================================================


@dataclass(frozen=True, slots=True)
class WritebackSummary:
    """回写状态的两个层级，**一起给**。

    | 层级 | 回答的问题 | 取哪个 |
    | --- | --- | --- |
    | 任务级 | 这张单子写成了没有 | `approval_tasks.write_status` |
    | 尝试级 | **最近这一次**为什么没成 | `comment_logs` 里最新的一行 |

    ⚠️ 两者**不可互相替代**（术语表：「写入状态」与「回写尝试」）：
    任务级是 `failed` 时，只知道"没写成"；一条 `not_written` +
    `MANUAL_CONFIRM_REQUIRED` 的尝试才回答了"等一次人工确认"。
    只给任务级，使用者只能看到"失败了"然后去重试 —— 而被拒的重试**永远是白试**。

    `attempt_no` 与尝试状态**一起**返回：任务是可能被重试的，
    一个没有序号的原因说不清"这是第几次的原因"。
    """

    task_write_status: str
    latest_attempt_id: int | None
    latest_attempt_no: int | None
    latest_attempt_status: str | None
    latest_reason_code: str | None
    latest_reason_text: str | None
    latest_attempt_at: datetime | None

    @property
    def latest_attempt_rejected(self) -> bool:
        """最近一次尝试是不是**门禁拒绝**（没发起写入）。

        判据是"状态是 `not_written` 但确实有一行尝试记录"：
        没有尝试记录时 `not_written` 只是"还没开始"，与"被拒"完全不同 ——
        两者都会让调用方去重试，而只有前者重试有用。
        """
        return (
            self.latest_attempt_id is not None
            and self.latest_attempt_status == WriteStatus.NOT_WRITTEN.value
        )


@dataclass(frozen=True, slots=True)
class WritebackAttempt:
    """一次回写尝试的要点（`comment_logs` 的一行）。"""

    attempt_id: int
    attempt_no: int
    status: str
    reason_code: str | None
    reason_text: str | None
    created_at: datetime | None


def latest_writeback_attempts(
    session: Session, task_ids: Sequence[int]
) -> dict[int, WritebackAttempt]:
    """一次取回**一批任务**各自的最近一次回写尝试。

    ⚠️ "最近一次"的判据是 `attempt_no DESC, id DESC`，**只有这一份实现**：
    `writeback_summary` 也走它。各写一遍时，分叉方式是某天只改了其中一处 ——
    于是列表与详情对同一条任务显示**不同的失败原因**，而两处都看起来对。

    列表页要用它（设计 §5.1 要求回写列同时给出状态与原因），
    而逐行查是 N+1。这里用一条窗口查询取回整页，成本与页大小无关。
    """
    ids = list(dict.fromkeys(int(task_id) for task_id in task_ids))
    if not ids:
        return {}

    ranked = (
        select(
            CommentLog.task_id.label("task_id"),
            CommentLog.id.label("attempt_id"),
            CommentLog.attempt_no.label("attempt_no"),
            CommentLog.write_status.label("status"),
            CommentLog.reason_code.label("reason_code"),
            CommentLog.reason_text.label("reason_text"),
            CommentLog.created_at.label("created_at"),
            func.row_number()
            .over(
                partition_by=CommentLog.task_id,
                order_by=(CommentLog.attempt_no.desc(), CommentLog.id.desc()),
            )
            .label("rank"),
        )
        .where(CommentLog.task_id.in_(ids))
        .subquery()
    )

    rows = session.execute(select(ranked).where(ranked.c.rank == 1)).all()
    return {
        int(row.task_id): WritebackAttempt(
            attempt_id=int(row.attempt_id),
            attempt_no=int(row.attempt_no),
            status=str(row.status),
            reason_code=row.reason_code,
            reason_text=row.reason_text,
            created_at=row.created_at,
        )
        for row in rows
    }


def writeback_summary_of(
    task: ApprovalTask, latest: WritebackAttempt | None
) -> WritebackSummary:
    """`(任务级状态, 最近一次尝试) → WritebackSummary` 的**纯映射**。

    拆成纯函数是为了让列表页能复用同一份映射（它已经批量取回了尝试），
    而不必为每一行再查一次 —— 两份映射代码迟早会对同一行给出不同的解释。
    """
    if latest is None:
        return WritebackSummary(
            task_write_status=task.write_status,
            latest_attempt_id=None,
            latest_attempt_no=None,
            latest_attempt_status=None,
            latest_reason_code=None,
            latest_reason_text=None,
            latest_attempt_at=None,
        )

    return WritebackSummary(
        task_write_status=task.write_status,
        latest_attempt_id=latest.attempt_id,
        latest_attempt_no=latest.attempt_no,
        latest_attempt_status=latest.status,
        latest_reason_code=latest.reason_code,
        latest_reason_text=latest.reason_text,
        latest_attempt_at=latest.created_at,
    )


def writeback_summary(session: Session, task: ApprovalTask) -> WritebackSummary:
    """任务级写入状态 + 最近一次尝试的原因（单条任务的入口）。"""
    return writeback_summary_of(
        task, latest_writeback_attempts(session, [task.id]).get(task.id)
    )


@dataclass(frozen=True, slots=True)
class TaskChain:
    """任务在当前时刻的**链路指针**（每一步取"最新的那个"）。

    ⚠️ 全部**现算**，不落库。缓存成列时，"重跑解析"要记得同时更新它 ——
    漏掉的那一次会让详情页永远指着一份旧解析，而页面看起来完全正常。
    """

    latest_parse_id: int | None
    latest_run_id: int | None
    current_result_id: int | None
    attachment_count: int
    correlation_id: str | None


def task_chain(session: Session, task: ApprovalTask) -> TaskChain:
    """任务 → 最新解析 / 最新批次 / 当前结果 / 附件数 / 最近关联 ID。"""
    latest_parse_id = session.execute(
        select(ContractParse.id)
        .where(ContractParse.task_id == task.id)
        .order_by(ContractParse.id.desc())
        .limit(1)
    ).scalar_one_or_none()

    latest_run_id = session.execute(
        select(ReviewRun.id)
        .where(ReviewRun.task_id == task.id)
        .order_by(ReviewRun.version_no.desc(), ReviewRun.id.desc())
        .limit(1)
    ).scalar_one_or_none()

    # ⚠️ "当前结果"的判据与列表页**共用** `current_results`（见那里的说明）：
    # 各写一遍时，分叉方式是某天只改了其中一处 —— 于是同一条任务
    # 在列表与详情上显示**不同的风险等级**，而两处都看起来对。
    current = current_results(session, [task.id]).get(task.id)
    current_result_id = None if current is None else current.result_id

    # 与列表页**共用**同一个计数实现（见 `attachment_counts`）：
    # 各写一遍时，"列表说 1 个、详情说 2 个"这种不一致没人查得出来。
    attachment_count = attachment_counts(session, [task.id]).get(task.id, 0)

    # 最近一次的关联 ID：从**作业**取（它是唯一能同时覆盖"入队过"与
    # "Worker 执行过"两类事件的表）。取不到就如实给 `None` ——
    # 编一个 UUID 出来会让排障的人拿着一个查不到任何日志的 ID 去查。
    correlation_id = session.execute(
        select(WorkflowJob.correlation_id)
        .where(
            WorkflowJob.task_id == task.id,
            WorkflowJob.correlation_id.is_not(None),
        )
        .order_by(WorkflowJob.id.desc())
        .limit(1)
    ).scalar_one_or_none()

    return TaskChain(
        latest_parse_id=latest_parse_id,
        latest_run_id=latest_run_id,
        current_result_id=current_result_id,
        attachment_count=attachment_count,
        correlation_id=correlation_id,
    )


# ============================================================
# 作业
# ============================================================


def list_jobs(
    session: Session,
    *,
    tenant_id: str,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    task_id: int | None = None,
    job_status: str | None = None,
) -> Page:
    """作业列表（**跨任务**，需按租户过滤）。

    ⚠️ 必须 join 回任务：`workflow_jobs` 自己没有 `tenant_id`，
    只看这张表等于把全部租户的作业混在一起返回。
    """
    stmt = (
        select(WorkflowJob)
        .join(ApprovalTask, ApprovalTask.id == WorkflowJob.task_id)
        .where(_task_scope(tenant_id))
    )

    job_status = check_member(job_status, JOB_STATUSES, parameter="job_status")
    if task_id is not None:
        stmt = stmt.where(WorkflowJob.task_id == task_id)
    if job_status is not None:
        stmt = stmt.where(WorkflowJob.job_status == job_status)

    stmt = stmt.order_by(WorkflowJob.id.desc())
    return _page(session, stmt, page=page, page_size=page_size)


def job_tenant_id(session: Session, job: WorkflowJob) -> str | None:
    """作业所属任务的租户（`None` 表示任务已不存在）。"""
    return session.execute(
        select(ApprovalTask.tenant_id).where(ApprovalTask.id == job.task_id)
    ).scalar_one_or_none()


def task_of(session: Session, task_id: int) -> ApprovalTask | None:
    """按 id 取任务（**不过滤租户**）。调用方必须自己判归属。

    单独留一个"不过滤"的入口，是为了让需要它的地方（解析 / 结果等
    已有端点的归属判断）写出**显式**的一行，而不是各自复制一遍 join ——
    复制的那些副本里，漏掉过滤的那一份不会有任何症状。
    """
    return session.get(ApprovalTask, task_id)


def assert_visible(task: ApprovalTask | None, *, tenant_id: str, what: str, ref: Any) -> ApprovalTask:
    """归属校验：任务不存在或不属于本租户 → 404（与不存在不可区分）。

    Raises:
        PermanentError: `RESOURCE_NOT_FOUND`。
    """
    if task is None or task.tenant_id != tenant_id:
        raise PermanentError(
            f"{what} {ref} 不存在", code=ErrorCode.RESOURCE_NOT_FOUND
        )
    return task


# ============================================================
# 规则评价
# ============================================================

#: 评价的**默认排序权重**：需要人看的排前面。
#:
#: ⚠️ 这是**排序**，不是**过滤**：`not_hit` 与 `not_applicable` 一条都不能少。
#: 只返回 `hit` 时，"这条规则为什么没报警"就永远答不出来 ——
#: 而那正是四态记录存在的理由。
_ATTENTION_FIRST = case(
    (RuleEvaluation.evaluation_status == EvaluationStatus.HIT.value, 0),
    (RuleEvaluation.evaluation_status == EvaluationStatus.NEEDS_REVIEW.value, 1),
    (RuleEvaluation.evaluation_status == EvaluationStatus.NOT_HIT.value, 2),
    else_=3,
)


def list_evaluations(
    session: Session,
    *,
    tenant_id: str,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    run_id: int | None = None,
    evaluation_status: str | None = None,
) -> Page:
    """规则评价列表。

    默认排序把 `hit` 与 `needs_review` 排前（`_ATTENTION_FIRST`），
    同权重内按 `rule_code` 稳定排序，最后以 `id` 兜底。

    ⚠️ **稳定排序在这里比别处更要紧**：同一批次的评价是**一次事务里批量写入**的，
    它们的 `created_at` 极可能完全相同。只按 `rule_code` 排也不够 ——
    规则码理论上可重（改版后的历史评价仍留着）。真正唯一的是 `id`。
    """
    stmt = (
        select(RuleEvaluation, ReviewRule.rule_code)
        .join(ReviewRule, ReviewRule.id == RuleEvaluation.rule_id)
        .join(ApprovalTask, ApprovalTask.id == RuleEvaluation.task_id)
        .where(_task_scope(tenant_id))
    )

    evaluation_status = check_member(
        evaluation_status, EVALUATION_STATUSES, parameter="evaluation_status"
    )
    if run_id is not None:
        stmt = stmt.where(RuleEvaluation.run_id == run_id)
    if evaluation_status is not None:
        stmt = stmt.where(RuleEvaluation.evaluation_status == evaluation_status)

    stmt = stmt.order_by(
        _ATTENTION_FIRST,
        ReviewRule.rule_code.asc(),
        RuleEvaluation.id.asc(),
    )
    # ⚠️ 这里选的是 `(RuleEvaluation, rule_code)` **两列**，因此要的是元组而不是标量。
    return _page(session, stmt, page=page, page_size=page_size, scalars=False)


# ============================================================
# 审查结果
# ============================================================


def list_results(
    session: Session,
    *,
    tenant_id: str,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    task_id: int | None = None,
) -> Page:
    """结果列表。

    ⚠️ **没有 `only_current` 这种过滤参数。** 加过一版，删掉了：
    "只给当前版本"要么在 SQL 里实现（同一任务取 `max(version_no)`，
    对每条任务都要一次相关子查询），要么在取完一页之后过滤 ——
    而后者会让 `total` 变成"过滤前的总数"，于是"共 8 条"配着 3 行数据。
    需要这个视角的调用方按任务取详情（详情里 `is_current_version` 逐行给出）。

    ⚠️ `confirmation_valid` **不在这里算**：它要求"该版本仍是任务当前版本"，
    路由层用 `result_service` 的同一口径逐行判定。
    这条 N+1 是刻意接受的（默认 20 行，正确性优先于这点开销）——
    写成"列表用简化口径、详情用完整口径"才是真正要避免的：
    两者对同一行会给出不同的答案。
    """
    stmt = select(ReviewResult).where(
        ReviewResult.task_id.in_(
            select(ApprovalTask.id).where(_task_scope(tenant_id))
        )
    )

    if task_id is not None:
        stmt = stmt.where(ReviewResult.task_id == task_id)

    stmt = stmt.order_by(
        ReviewResult.created_at.desc(),
        ReviewResult.id.desc(),
    )
    return _page(session, stmt, page=page, page_size=page_size)


# ============================================================
# 运行日志与审计事件（M7 / Task 5）
# ============================================================


def list_task_logs(
    session: Session,
    *,
    task_id: int,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    log_level: str | None = None,
    log_type: str | None = None,
    correlation_id: str | None = None,
) -> Page:
    """某任务的运行日志（**按时间倒序**，最新在前）。

    ⚠️ **必须支持 `correlation_id` 过滤**：它是"一次审批从拉取到回写可完整追踪"
    的落地点。全链路日志散在 API 与 Worker 两个进程里，`correlation_id`
    是唯一能把它们重新拼起来的键 —— 而这个接口不给这个过滤条件时，
    排障的人只能拉全表再自己筛。

    ⚠️ 本函数**不过租户门**：调用方（路由）必须先经 `get_task` 拿到任务
    （那里已经有 404 语义），否则"知道 task_id 就能读到别人的日志"。
    """
    stmt = select(TaskLog).where(TaskLog.task_id == task_id)

    log_level = check_member(log_level, LOG_LEVELS, parameter="log_level")
    log_type = check_member(log_type, LOG_TYPES, parameter="log_type")
    if log_level is not None:
        stmt = stmt.where(TaskLog.log_level == log_level)
    if log_type is not None:
        stmt = stmt.where(TaskLog.log_type == log_type)
    if correlation_id is not None:
        stmt = stmt.where(TaskLog.correlation_id == correlation_id)

    stmt = stmt.order_by(TaskLog.created_at.desc(), TaskLog.id.desc())
    return _page(session, stmt, page=page, page_size=page_size)


def list_audit_events(
    session: Session,
    *,
    tenant_id: str,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    task_id: int | None = None,
    action: str | None = None,
    include_system: bool = False,
) -> Page:
    """审计事件（只读，按时间倒序）。

    ## 系统级事件为什么需要显式开关

    M7 起 `audit_events.task_id` **可为空**：规则变更影响所有任务，
    它不属于任何一条任务。于是"按租户过滤"对它没有答案，只有两个选择：

    | 选择 | 后果 |
    | --- | --- |
    | 默认带上系统级事件 | 每个租户都能看到全局配置变更 —— 多租户下是越权 |
    | **默认不带**（本实现） | 调用方想看时显式传 `include_system=true` |

    默认值选"看不到"是 fail-closed 的方向：漏看一条事件只是少一点信息，
    而多看到别人的东西是数据泄漏。需要全局视图的运维显式打开即可。

    ⚠️ 任务归属经 `audit_events.task_id → approval_tasks.tenant_id` 判定，
    与 `list_results` 同一手法（审计表自己没有 `tenant_id`）。
    """
    scoped_tasks = select(ApprovalTask.id).where(_task_scope(tenant_id))

    if task_id is not None:
        # 指定任务时**只**看该任务 —— 且要求它属于本租户（`get_task` 会 404）。
        get_task(session, tenant_id=tenant_id, task_id=task_id)
        stmt = select(AuditEvent).where(AuditEvent.task_id == task_id)
    elif include_system:
        stmt = select(AuditEvent).where(
            or_(AuditEvent.task_id.in_(scoped_tasks), AuditEvent.task_id.is_(None))
        )
    else:
        stmt = select(AuditEvent).where(AuditEvent.task_id.in_(scoped_tasks))

    action = check_member(action, AUDIT_ACTIONS, parameter="action")
    if action is not None:
        stmt = stmt.where(AuditEvent.action == action)

    stmt = stmt.order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
    return _page(session, stmt, page=page, page_size=page_size)


# ============================================================
# 立场确认（Context Confirmation）
# ============================================================


def confirm_context(
    session: Session,
    *,
    tenant_id: str,
    task_id: int,
    actor: Any,
    correction: Mapping[str, str] | None = None,
) -> ApprovalTask:
    """人工确认**权威审查上下文**（我方 / 合同标签 / 业务角色 / 合同类型）。

    ## 与"结果确认"是两件事

    本函数确认的是**审查立场**（`context_status`），
    `result_service.confirm_result` 确认的是**审查结果与回写正文**。
    术语表里两者被明确分开：立场对了、结果未必认可；
    结果认可、立场未必对。合并成一个动作时，前端只能把两者一起勾选 ——
    于是"我只想确认立场，结果还要再看看"这件事**做不到**。

    ## 两条路：**确认**既有立场 / 人工**给出**立场

    | 传入 | 允许的状态 | 效果 |
    | --- | --- | --- |
    | 无 `correction` | **只有** `complete` | 确认审批系统给出的立场 |
    | 有 `correction` | **任意**（含 `missing` / `conflict`） | 写入四值 + 确认（人工背书） |

    **无 `correction` 时只允许 `complete`**：那时没有可确认的对象 ——
    `missing` 连我方名称都没有，`conflict` 是两个来源互相矛盾。
    放行等于让人确认一个**我们说不清是什么**的东西，
    而回写门禁随后会把它当成可信立场使用。

    **有 `correction` 时任意状态都可以**：这次是人**给出**立场，而不是认可它。
    这不是"放宽"，而是补上一条原本缺失的路径 ——
    `missing` 是刚拉取完任务的**正常**状态（M3 已定），
    没有这条路时它永久停住（确认被拒、又填不进去），而回写门禁要求可信立场。
    `conflict` 下带 `correction` 即人工**裁定**（设计 §4.2 要求必须能裁定）。

    两条路都**清空 `context_conflict_json`**：人工背书就是裁定，
    留着冲突记录会让下一个人以为冲突还在。

    ## 幂等

    重复确认不换时间、不换人、不追加审计事件 —— 同一人对同一份立场确认两次
    是**一次**业务事实，不是两次。审计账要能按动作聚合统计，
    重复计数会让"本周确认了多少份"这个数字直接失真。

    ⚠️ 修正时审计里带 `changed_fields`：事后要能回答"人工改了什么"，
    而只记一份新值说不清改的是哪几条（也可能一条都没改）。

    Raises:
        PermanentError: 任务不存在 / 不属于本租户（`RESOURCE_NOT_FOUND`）。
        ValueError: 无 `correction` 且上下文状态不是 `complete`（→ 400）。
    """
    from app.models import AuditEvent

    task = get_task(session, tenant_id=tenant_id, task_id=task_id)

    changed_fields: list[str] = []

    if correction is None:
        if task.context_status == ContextStatus.CONFIRMED.value:
            return task

        if task.context_status != ContextStatus.COMPLETE.value:
            raise ValueError(
                f"任务 {task_id} 的权威上下文状态是 {task.context_status!r}，"
                f"只有 {ContextStatus.COMPLETE.value!r} 才可确认："
                f"{ContextStatus.MISSING.value!r} 表示我方立场还没拿到，"
                f"{ContextStatus.CONFLICT.value!r} 表示两个来源互相矛盾 —— "
                "两者都没有可确认的对象。"
                "（要直接给出这四条业务事实，请带请求体提交修正。）"
            )
    else:
        # `_CONTEXT_FIELDS` 之外的键**不可能**到达这里（请求体是 `extra="forbid"`
        # 的模型），因此不必再防一遍；这里只比较差异，用于审计与幂等判断。
        changed_fields = sorted(
            name for name, value in correction.items() if getattr(task, name) != value
        )
        if task.context_status == ContextStatus.CONFIRMED.value and not changed_fields:
            # 已确认且四条都没变 → 幂等：不换人、不追加审计事件
            return task
        for name, value in correction.items():
            setattr(task, name, value)

    # 人工背书就是裁定：冲突记录必须清掉，否则下一个人会以为冲突还在
    # （清掉之后 `context_status` 立刻回到 `confirmed`，回写门禁重新放行）
    task.context_conflict_json = None
    task.context_status = ContextStatus.CONFIRMED.value
    # ⚠️ 来源改成 `manual`：确认这个动作本身**就是**"这四条业务事实由人工背书"。
    # 留成 `approval_system` 时，事后无法区分"审批系统给的就是这样"
    # 与"人工看过并认可" —— 而回写门禁把 `confirmed` 当可信立场的理由，
    # 恰恰是后者。
    task.context_source = ContextSource.MANUAL.value

    session.add(
        AuditEvent(
            task_id=task.id,
            actor_id=actor.actor_id,
            actor_name=actor.display_name,
            # ⚠️ 取枚举而不是写字符串字面量：`AuditAction` 是取值域的唯一定义处，
            # 字面量在枚举改名时会**静默分叉** —— 而分叉后
            # `tests/test_m6_schema.py` 比对的是枚举与 DDL，两边都是新值，
            # 因此这条写错的调用不会被任何一致性检查发现。
            action=AuditAction.CONTEXT_CONFIRMED.value,
            target_type="approval_task",
            target_id=task.id,
            correlation_id=_correlation_id(),
            detail_json=_compact_json(
                {
                    "task_id": task.id,
                    "our_party_name": task.our_party_name,
                    "our_party_contract_label": task.our_party_contract_label,
                    "our_party_business_role": task.our_party_business_role,
                    "contract_type": task.contract_type,
                    # 人工**改了哪几条**（纯确认时为空）：只记一份新值说不清
                    # 改的是哪几条 —— 也可能一条都没改
                    "changed_fields": changed_fields,
                }
            ),
        )
    )
    session.flush()
    return task


def _correlation_id() -> str | None:
    """当前请求的关联 ID（审计事件带上它，排障时能把一次操作串起来）。"""
    from app.context import get_correlation_id

    return get_correlation_id()


def _compact_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _json_or_none(raw: str | None) -> Any:
    """库里的 JSON 文本 → 对象；解析不了返回 `None` **而不是抛错**。

    一条损坏的记录不该把整个列表接口变成 500 —— 它的其他字段仍然有用。
    """
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


#: 便于调用方做参数白名单校验（路由层用它把非法枚举值挡成 400）。
TASK_STATUSES: frozenset[str] = frozenset(item.value for item in TaskStatus)
JOB_STATUSES: frozenset[str] = frozenset(item.value for item in JobStatus)
EVALUATION_STATUSES: frozenset[str] = frozenset(item.value for item in EvaluationStatus)
LOG_LEVELS: frozenset[str] = frozenset(item.value for item in LogLevel)
LOG_TYPES: frozenset[str] = frozenset(item.value for item in LogType)
AUDIT_ACTIONS: frozenset[str] = frozenset(item.value for item in AuditAction)
