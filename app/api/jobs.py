"""作业与解析结果的最小查询接口（M4 / §4.6 修-4 / 修-23）。

M4 **只提供这两个**；M7 再扩展为完整的任务/作业/日志查询。
把它们做小的理由是：`TaskRef` 的形状一旦承诺出去就改不动了，
而"作业状态与结果之间有一个可原子读取的关联"这件事必须有地方落。

## 为什么 `result_ref` 放在作业响应里，而不是另开一个"取结果"接口

**作业状态与结果之间需要一个可原子读取的关联。** 调用方拿到 `succeeded`
的那一刻，`parse_id` 必须已经确定（§4.4.3 的同事务保证这一点），
否则会出现"状态成功了但结果还不知道在哪"的窗口 —— 而调用方唯一的处置
就是轮询重试，把一个原子事实变成一段猜测。

## 为什么把 `basic_info` / `clause_info` 内联返回

工具 4 的契约是"**返回结构化字段**"（原始需求：
`parse_contract_document(document_id)`）。让调用方自己去对象存储取工件，
等于把这个契约推给每一个集成方，而**工件的布局（§3.4）是本系统的实现细节** ——
M9 换 MinIO 就会变成破坏性变更。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from sqlalchemy import select

from app.api.deps import get_db, require_permissions
from app.api.views import evaluation_json, iso, json_or_none, page_json
from app.auth import Actor, Permission
from app.enums import ErrorCode, JobStatus, JobType
from app.errors import PermanentError
from app.models import (
    ApprovalTask,
    CommentLog,
    ContractParse,
    OutboxEvent,
    ReviewRun,
    WorkflowJob,
)
from app.rules.aggregator import aggregate
from app.rules.evaluator import RuleEvaluation as Evaluation
from app.services import query_service
from app.services.rule_service import evaluations_of_run, rule_names_by_code

router = APIRouter(prefix="/api", tags=["作业、解析结果与审查结果（M4 / M6）"])


def _assert_task_visible(
    session: Session, task: ApprovalTask | None, *, actor: Actor, what: str, ref: object
) -> ApprovalTask:
    """嵌套资源的归属校验：任务不存在或不属于本租户 → **404**。

    ⚠️ 这个函数存在的理由，是"嵌套 id 也要过同一道门"这件事
    在**每一处**都写一遍时必然有一处漏掉 —— 而漏掉的那一处
    不会有任何症状，只是安静地多返回一份别的租户的数据。
    统一走 `query_service.assert_visible`，判据只有一份。

    用 404 而不是 403：403 等于确认"这个 id 存在"，于是状态码本身
    就成了逐位试出别人 id 的探针。
    """
    return query_service.assert_visible(
        task, tenant_id=actor.tenant_id, what=what, ref=ref
    )


@router.get(
    "/jobs",
    summary="作业列表（分页）",
    description=(
        "按 `id DESC` 返回本租户的作业。\n\n"
        "⚠️ `workflow_jobs` 表**没有** `tenant_id` —— 它的归属由 `task_id` 传递。\n"
        "只查这张表等于把全部租户的作业混在一起返回，因此这里必须 join 回任务。\n\n"
        "`task_id` / `job_status` 可选，取值都按白名单校验（拼错 → 400，不是空列表）。"
    ),
)
def list_jobs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(
        default=query_service.DEFAULT_PAGE_SIZE, ge=1, le=query_service.MAX_PAGE_SIZE
    ),
    task_id: int | None = Query(default=None),
    job_status: str | None = Query(default=None),
    session: Session = Depends(get_db),
    actor: Actor = Depends(require_permissions(Permission.TASK_READ)),
) -> dict[str, Any]:
    result = query_service.list_jobs(
        session,
        tenant_id=actor.tenant_id,
        page=page,
        page_size=page_size,
        task_id=task_id,
        job_status=job_status,
    )
    return page_json(result, _job_json)


@router.get(
    "/jobs/{job_id}",
    summary="查询作业状态（M4 最小接口）",
    description=(
        "返回作业状态、尝试次数、退避时间与失败原因。\n\n"
        "**`result_ref` 在作业成功时非空**，指向本次作业产生的解析结果 ——\n"
        "调用方拿到 `succeeded` 时 `parse_id` 必然已经确定（§4.4.3 同事务），"
        "因此这里不存在\"状态成功了但结果还不知道在哪\"的窗口。\n\n"
        "M7 会在此基础上扩展为完整的任务/作业/日志查询，"
        "但**不改变** `TaskRef` 的形状。"
    ),
)
def get_job(
    job_id: int,
    session: Session = Depends(get_db),
    actor: Actor = Depends(require_permissions(Permission.TASK_READ)),
) -> dict[str, Any]:
    job = session.get(WorkflowJob, job_id)
    if job is None:
        # ⚠️ 用 `RESOURCE_NOT_FOUND` 而不是 `TASK_NOT_FOUND`：
        # 后者是**业务结论**（"还没拉取，先去拉一次"），
        # 这里的正确处置是**核对 id**。混用会让调用方拿着不存在的
        # job_id 去做一次无用的拉取。
        raise PermanentError(
            f"作业 {job_id} 不存在", code=ErrorCode.RESOURCE_NOT_FOUND
        )

    # 嵌套 id 也要过租户门：作业状态、失败原因、关联的 parse_id 单独看都不敏感，
    # 但它们是**逐层拼出对方数据结构的碎片**。
    _assert_task_visible(
        session,
        query_service.task_of(session, job.task_id),
        actor=actor,
        what="作业",
        ref=job_id,
    )
    return _job_json(job)


def _job_json(job: WorkflowJob) -> dict[str, Any]:
    """作业的对外形状 —— 列表与详情**共用**。"""
    return {
        "job_id": job.id,
        "task_id": job.task_id,
        "job_type": job.job_type,
        "job_status": job.job_status,
        "attempt_no": job.attempt_no,
        "max_attempts": job.max_attempts,
        "next_retry_at": iso(job.next_retry_at),
        "last_error_code": job.last_error_code,
        "last_error_text": job.last_error_text,
        # 关联 ID：Worker 在另一个进程里，它是唯一的追踪线索
        "correlation_id": job.correlation_id,
        "status_url": f"/api/jobs/{job.id}",
        "result_ref": _result_ref(job),
    }


def _result_ref(job: WorkflowJob) -> dict[str, Any] | None:
    """成功时指向结果；其余情况为 `None`。

    ⚠️ 判据是 `job_status == 'succeeded'`（**作业级成功**），
    不是 `parse_status == 'succeeded'`。作业成功但解析被质量门禁判 `failed`
    是完全可能的 —— 那时作业确实是"跑完了"，而**结果不可用**。
    因此 `result_ref` 指过去之后，`/api/parses/{id}` 仍可能返回
    `failed` / `blocked` 与它的错误码。调用方**必须**看 `parse_status`，
    不能把 `result_ref` 非空当成"解析成功"。
    """
    if job.job_status != JobStatus.SUCCEEDED.value:
        return None

    # ⚠️ 按 `job_type` 分派，而不是"试着从输入里找个 id"：
    # 两种作业的结果是**不同的资源**，而"取到哪个 id 就用哪个"会在
    # 作业输入里恰好同时有 parse_id 与 run_id 时指向错误的那个 —— 且不报错。
    if job.job_type == JobType.PARSE.value:
        parse_id = _input_id(job, "parse_id")
        if parse_id is None:
            return None
        return {"parse_id": parse_id, "result_url": f"/api/parses/{parse_id}"}

    if job.job_type == JobType.RULE.value:
        # 批次在**入队时**就建好了（与工具 4 预留解析占位同理），
        # 因此这里能直接给出结果地址 —— 调用方拿到 `succeeded` 即可取聚合结论。
        run_id = _input_id(job, "run_id")
        if run_id is None:
            return None
        return {"run_id": run_id, "result_url": f"/api/runs/{run_id}"}

    # ---- RESULT / WRITEBACK：**没有**结果引用（M6）----
    #
    # ⚠️ 这两个分支刻意返回 `None`，而且刻意**写出来**而不是让它落到下面的
    # `return None`。理由是这里存在一个很自然的错误写法：
    #
    #     输入里有个 result_id → 就拿它当结果引用
    #
    # 它"能跑通、看起来也对"，但指向的是**输入的那个**结果 ——
    # 也就是回写作业的目标结果，而不是这个作业产生的任何东西。
    # 于是一次作业失败后，调用方照着 `result_ref` 去看，看到一份**完全正常**的结果，
    # 而真正出问题的那次保存/回写没有任何线索指向它。
    #
    # 根本原因：RESULT / WRITEBACK 作业的产物 id（结果号 / 尝试号）**不在冻结输入里** ——
    # 它们是作业**执行时**才产生的，而 `input_json` 在入队那一刻就被冻结、
    # 且处理器无权写检查点（见 `app/worker.py` 的 `JobRun`）。
    # 与其猜，不如如实回答"没有"。
    #
    # 调用方要拿产物 id，走**同步路径**：工具 6 直接返回 `result_id`，
    # 工具 7 直接返回 `attempt_id`。作业接口对这两类只说"跑完了没有"。
    if job.job_type in (JobType.RESULT.value, JobType.WRITEBACK.value):
        return None

    return None


def _input_id(job: WorkflowJob, name: str) -> int | None:
    """从作业输入里取一个整数 id（`parse_id` / `run_id`）。

    `input_json` 是**已校验并冻结**的输入（`job_inputs` 的模型），
    因此这里读得到；读不到只可能是旧数据或手工改库 —— 返回 `None`
    而不是抛错：作业状态本身仍然是有用的信息。
    """
    try:
        payload = json.loads(job.input_json)
    except (TypeError, ValueError):
        return None
    value = payload.get(name) if isinstance(payload, dict) else None
    return value if isinstance(value, int) else None


def parse_row_json(parse: ContractParse) -> dict[str, Any]:
    """解析记录的对外形状 —— **列表与详情共用**（M8 Task 3/5）。

    ⚠️ 分成两份时，分叉方式是"某天只给详情加了一个字段"，
    而使用者看到的是**同一条解析在两张页面上字段不一样** —— 没有任何一处会报错。
    这正是 `views.page_json` / `job_json` 已经在用的做法。

    ⚠️ 刻意叫 `attachment_record_id` 而不是 `attachment_id`（§4.6 的字面写法）：
    库里的 `contract_parses.attachment_id`（INTEGER，**本系统主键**）与
    `approval_attachments.attachment_id`（TEXT，**外部编号**）**同名不同义**。
    对外接口沿用那个名字，等于把这个歧义出口给每一个集成方 ——
    而 §4.5 修-15 改名 `attachment_record_id` 要解决的正是这件事。

    `basic_info` / `clause_info` **原样下发**：它们是
    `{schema_version, fields: [...]}`，每个字段带 `status`（四态）与
    `evidence`（页号 / bbox / 字符区间 / 两个精度）——
    M8 的"字段四态分别呈现"与"证据定位"直接读它们，
    **不另开 `/fields` 端点**（同一份数据两条获取路径迟早不一致）。
    """
    return {
        "parse_id": parse.id,
        "task_id": parse.task_id,
        "attachment_record_id": parse.attachment_id,
        "parse_status": parse.parse_status,
        "parse_version": parse.parse_version,
        "parser_name": parse.parser_name,
        "parser_version": parse.parser_version,
        "cache_key": parse.cache_key,
        "parse_error_code": parse.parse_error_code,
        "parse_error": parse.parse_error,
        "basic_info": json_or_none(parse.basic_info_json),
        "clause_info": json_or_none(parse.clause_info_json),
        "quality": {
            "text_coverage": parse.text_coverage,
            "ocr_pages": parse.ocr_pages,
            "ocr_confidence": parse.ocr_confidence,
        },
    }


@router.get(
    "/tasks/{task_id}/parses",
    summary="某任务的解析版本列表（新版在前）",
    description=(
        "返回该任务历次解析的版本列表，**每行的形状与 `GET /api/parses/{id}` 完全一致**"
        "（列表里已经带 `quality`，因此界面不必为每一行再发一次详情请求）。\n\n"
        "用途是**版本切换**（验收 12）：切换时字段与 PDF 必须同时切换 ——"
        "而「当前选中的是哪个版本」由这里的 `parse_version` 决定，"
        "不稳定的排序会让默认选中项在两次请求之间变化。\n\n"
        "⚠️ **字段的四态与证据不在这里另开接口**：它们内联在每行的 "
        "`basic_info` / `clause_info` 里（`{schema_version, fields: [...]}`，"
        "每个字段带 `status` 与 `evidence`）。再开一个 `/fields` 端点"
        "会让同一份数据有两条获取路径，而两条路径的字段迟早不一致。\n\n"
        "跨租户与任务不存在都返回 **404**（不给枚举线索）。"
    ),
)
def list_task_parses(
    task_id: int,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(
        default=query_service.DEFAULT_PAGE_SIZE, ge=1, le=query_service.MAX_PAGE_SIZE
    ),
    session: Session = Depends(get_db),
    actor: Actor = Depends(require_permissions(Permission.TASK_READ)),
) -> dict[str, Any]:
    # 先过归属门（不存在 / 跨租户 → 404），再列版本 ——
    # 顺序反了会得到"别人的任务返回空列表"，而空列表读起来是"这个任务还没解析过"
    query_service.get_task(session, tenant_id=actor.tenant_id, task_id=task_id)
    result = query_service.list_parses(
        session,
        tenant_id=actor.tenant_id,
        task_id=task_id,
        page=page,
        page_size=page_size,
    )
    return page_json(result, parse_row_json)


@router.get(
    "/parses/{parse_id}",
    summary="读取解析结果（M4 最小接口）",
    description=(
        "返回解析记录的全部对外可见内容：状态、**机器错误码 + 人读文本**、\n"
        "结构化字段（`basic_info` / `clause_info`）与质量指标。\n\n"
        "**字段直接内联返回**，不要求调用方去对象存储取工件 ——\n"
        "工件布局是本系统的实现细节，M9 换 MinIO 不应成为破坏性变更。\n\n"
        "⚠️ `parse_error_code` 是**稳定机器码**，`parse_error` 是给人看的文本。"
        "历史失败记录靠前者才可统计（§3.2 修-30）。"
    ),
)
def get_parse(
    parse_id: int,
    session: Session = Depends(get_db),
    actor: Actor = Depends(require_permissions(Permission.TASK_READ)),
) -> dict[str, Any]:
    parse = session.get(ContractParse, parse_id)
    if parse is None:
        raise PermanentError(
            f"解析记录 {parse_id} 不存在", code=ErrorCode.RESOURCE_NOT_FOUND
        )

    _assert_task_visible(
        session,
        query_service.task_of(session, parse.task_id),
        actor=actor,
        what="解析记录",
        ref=parse_id,
    )

    # 对外形状由 `parse_row_json` 定义（**列表与详情共用**）——
    # 字段含义与"为什么不叫 attachment_id"的说明都在那里
    return parse_row_json(parse)


@router.get(
    "/runs/{run_id}",
    summary="读取审查批次结果（M5）",
    description=(
        "返回批次的六项版本绑定、**现算的聚合结论**（总风险 / 结论完整性 / 四态计数 /\n"
        "关注点候选）与逐条评价。\n\n"
        "⚠️ **聚合是现算的，不是从结果表读的**（决策 ⑦）：它由本批次的 `rule_hits`\n"
        "重新算出，因此不存在\"结论与依据各自漂移\"。M6 才落 `review_results`。\n\n"
        "⚠️ `run_status != 'completed'` 时，聚合只反映了**已经落库的那部分**评价。\n"
        "调用方应看 `run_status`（或作业状态）再解读 —— 把半截批次当成完整结论，\n"
        "会得到一份\"低风险\"，因为我们**还没算完**。\n\n"
        "`ruleset_version` 是**快照的摘要**：批次自带当时的规则集内容\n"
        "（`review_runs.ruleset_snapshot_json`），规则日后被改也能还原判断依据。"
    ),
)
def get_run(
    run_id: int,
    session: Session = Depends(get_db),
    actor: Actor = Depends(require_permissions(Permission.TASK_READ)),
) -> dict[str, Any]:
    run = session.get(ReviewRun, run_id)
    if run is None:
        raise PermanentError(
            f"审查批次 {run_id} 不存在", code=ErrorCode.RESOURCE_NOT_FOUND
        )

    _assert_task_visible(
        session,
        query_service.task_of(session, run.task_id),
        actor=actor,
        what="审查批次",
        ref=run_id,
    )

    evaluations = evaluations_of_run(session, run_id)
    summary = aggregate(evaluations)
    # 界面给人看的是规则**名字**，编码是给机器/排障的 —— 在 API 层补齐，
    # 不为此惊动领域对象（`Evaluation` 保持纯判断结果）
    rule_names = rule_names_by_code(session, [item.rule_code for item in evaluations])

    return {
        "run_id": run.id,
        "task_id": run.task_id,
        "parse_id": run.parse_id,
        "version_no": run.version_no,
        "run_status": run.run_status,
        "ruleset_version": run.ruleset_version,
        "model_version": run.model_version,
        "prompt_version": run.prompt_version,
        "config_version": run.config_version,
        "aggregate": summary.to_json(),
        "evaluations": [_evaluation_json(item, rule_names) for item in evaluations],
        "started_at": iso(run.started_at),
        "finished_at": iso(run.finished_at),
    }


# ⚠️ `/api/results/{id}` **不再定义在本模块**。M7 把结果与规则评价的读写
# 收进了 `app/api/results.py`：本模块是"作业与解析结果"，而结果（连同它的
# 确认接口）是另一条链路。两条路径同时注册时，先注册的那一条赢，
# 后一条**永远不生效且不报错** —— 一个只在删掉前一条时才被发现的分叉。


@router.get(
    "/writebacks/{attempt_id}",
    summary="查询回写尝试（M6）",
    description=(
        "返回一次回写尝试的状态与**投递进度**。\n\n"
        "⚠️ 这里有两个层级，不要混：\n\n"
        "| 层级 | 字段 | 回答 |\n"
        "| --- | --- | --- |\n"
        "| **尝试**（`comment_logs`） | `write_status` | 这次回写走到哪一步 |\n"
        "| **投递**（`outbox_events`） | `delivery` | 派发器试了几次、下次什么时候、上次为什么失败 |\n\n"
        "只给尝试状态的话，一次卡在重试中的回写只能看到 `writing` ——"
        "而调用方最需要知道的「还要等多久 / 是不是快耗尽了」恰好在投递层。\n\n"
        "`write_status=not_written` + `reason_code` 表示**门禁拒绝**："
        "没有发起过回写，`delivery` 为 `null`（拒绝不创建 Outbox 事件）。\n\n"
        "尝试不存在 → 404 + `RESOURCE_NOT_FOUND`（与 `/api/jobs/{id}` 同一约定）。"
    ),
)
def get_writeback(
    attempt_id: int,
    session: Session = Depends(get_db),
    actor: Actor = Depends(require_permissions(Permission.TASK_READ)),
) -> dict[str, Any]:
    attempt = session.get(CommentLog, attempt_id)
    if attempt is None:
        raise PermanentError(
            f"回写尝试 {attempt_id} 不存在", code=ErrorCode.RESOURCE_NOT_FOUND
        )

    task = _assert_task_visible(
        session,
        query_service.task_of(session, attempt.task_id),
        actor=actor,
        what="回写尝试",
        ref=attempt_id,
    )
    event = session.execute(
        select(OutboxEvent).where(OutboxEvent.idempotency_key == attempt.idempotency_key)
    ).scalar_one_or_none()

    return {
        "attempt_id": attempt.id,
        "task_id": attempt.task_id,
        "result_id": attempt.review_id,
        "instance_id": None if task is None else task.instance_id,
        "write_status": attempt.write_status,
        # 机器判据与人读文本都给：只给码则界面只能显示代号，
        # 只给文本则调用方没法分支（见 `app/models.py` 对这两个字段的分工）。
        "reason_code": attempt.reason_code,
        "reason_text": attempt.reason_text,
        "content_digest": attempt.content_digest,
        "attempt_no": attempt.attempt_no,
        "operator_name": attempt.operator_name,
        "created_at": iso(attempt.created_at),
        # 任务级状态（回写的上游事实：任务是不是已回写成功）
        "task_status": None if task is None else task.task_status,
        "task_write_status": None if task is None else task.write_status,
        "delivery": _delivery_json(event),
        "status_url": f"/api/writebacks/{attempt.id}",
    }


def _delivery_json(event: OutboxEvent | None) -> dict[str, Any] | None:
    """投递进度。**没有事件时返回 `None`，而不是一个全零的假对象。**

    门禁拒绝的尝试本来就没有 Outbox 事件（拒绝不产生意向）。用零值对象填充
    会让它看起来"事件存在但还没投递" —— 于是"被拒绝"与"排队中"在接口上
    长得一样，而两者的正确处置完全相反（一个要人去确认，一个只需等待）。
    """
    if event is None:
        return None
    return {
        "event_id": event.id,
        "event_type": event.event_type,
        "event_status": event.event_status,
        "attempt_no": event.attempt_no,
        "max_attempts": event.max_attempts,
        "next_retry_at": iso(event.next_retry_at),
        "last_error_code": event.last_error_code,
        "last_error_text": event.last_error_text,
        "correlation_id": event.correlation_id,
        "created_at": iso(event.created_at),
        "delivered_at": iso(event.delivered_at),
    }


def _evaluation_json(
    item: Evaluation, rule_names: dict[str, str]
) -> dict[str, Any]:
    """一条评价的对外形状。

    ⚠️ 形状本身在 `app/api/views.py` 里，**与 `/api/evaluations` 共用**：
    两处各写一遍时的分叉方式是"某天给其中一处加了一个字段"，
    而使用者看到的是同一条评价在两张页面上字段不一样 —— 没有一处会报错。
    这里只做"领域对象 → 那些参数"的取值。
    """
    return evaluation_json(
        rule_code=item.rule_code,
        rule_name=rule_names.get(item.rule_code),
        evaluation_status=item.status.value,
        risk_level=item.risk_level.value,
        reason_code=item.reason_code.value if item.reason_code else None,
        reason_text=item.reason_text,
        evidence_json=item.evidence_json,
        hit_detail=item.hit_detail,
    )
