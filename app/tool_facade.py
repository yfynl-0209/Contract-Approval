"""七个工具的**规范门面**（需求 2.4.10）。

## 为什么需要它

需求规定的 7 个工具名与最低参数**逐字不变**，而本系统的内部模型与需求
写下的那套词汇并不一一对应。门面就是这两者之间**唯一**的翻译点。

它同时解决第二件事：REST 与 MCP 必须**调用同一套业务实现**。
若各自演一遍，"业务逻辑"就有了两份，而它们的分叉方式是
"某一边忘了改" —— 表现为两种协议对同一份输入给出不同结论，
且没有任何一处看得出这是分叉。

因此：

    app/api/tools.py   （REST） ─┐
                                 ├─→ app/tool_facade.py → app/services/*
    app/mcp_server.py  （MCP） ─┘

## 本模块**不得** import `app.api` 或 `fastapi`

那会把 MCP 形态连带拖进 HTTP 框架：一个只需要 JSON 的适配器，
为了调用工具而必须构造请求对象、理解状态码。

## 签名：需求的名字，一条不少

门面函数的参数名与类型**照抄需求 2.4.10**（`case_id` / `review_id` /
`document_id` 都是字符串），内部再把它们翻译成主键。这不是"多支持一套别名"，
而是把兼容性**收在一个地方**：

    REST 路由（`app/api/tools.py`）**拒绝**这些旧名字（Pydantic `extra="forbid"` → 422），
    旧名字只活在门面上。见 `tests/test_m6_api.py::test_legacy_parameter_names_are_rejected_at_this_layer`。

若反过来只收 `int`，每一个照需求写代码的调用方（含 MCP 客户端）都得先学会
我们内部的类型 —— 那么"签名逐字不变"这句话就不再成立了。

## 参数名映射：需求的词汇 → 本系统的词汇

| 工具 | 需求参数 | 门面参数 | 指向本系统的什么 |
| --- | --- | --- | --- |
| 4 | `document_id` | `document_id` | `approval_attachments.id`（**本系统主键**，不是外部附件编号） |
| 5 | `case_id` | `case_id` | `contract_parses.id` —— 要审查的那次解析 |
| 6 | `case_id` | `case_id` | `review_runs.id` —— 审查批次 |
| 7 | `review_id` | `review_id` | `review_results.id` —— 审查结果 |

⚠️ **`case_id` 在工具 5 与工具 6 里指向两个不同的东西。** 这是需求自身的
重名：它把"案件"既当作规则审查的输入（工具 5），又当作结果保存的归属
（工具 6），而本系统在这两处之间插进了一层「批次」—— 规则跑在解析上，
结果属于批次。工具 6 的落点由 M6 计划固定（*"Tool 6 `case_id` means M5
`review_runs.id`"*）；工具 5 调用时批次**还不存在**（它正是要建批次的那个），
所以只能是解析。

这个重名被**显式记录**而不是用一个"聪明的自动判别"抹平：
自动判别会在传错 id 时**猜一个**，而两个 id 都是正整数 ——
猜对了没人知道，猜错了会去审查另一份合同，且看起来完全正常。

## 需求写 `str`，库里是 `int`

需求把三个 id 都定义为**字符串**，本系统的主键是 `int`。
门面只接受需求写下的那**一种**写法（十进制数字串），在 `_legacy_id` 这一处转换：

- 两种都收 → "该传哪个"变成一个没有正确答案的问题，而两种写法都会在某些
  调用路径上"看起来能用"（这条判据是 M6 定下的，见上面的测试）；
- 只收 `int` → 见上一节，需求签名就不成立了。

转换失败抛 `ValueError`（不是 `PermanentError`）：这正是本仓库
"调用方传来的标识不合法 → 400 + `INVALID_ARGUMENT`"的既有约定
（`app/main.py` 的 `ValueError` 处理器）。用 `PermanentError` 会落进
"其他确定性失败 → 500"，于是"id 写错了"看起来像"服务端崩了" ——
而正确处置只是核对一下 id。

## 返回形态

每个函数返回的 dict **就是**工具的响应体（可以直接 JSON 化）。
协议层只负责运输：REST 把它交给 FastAPI，MCP 把它塞进工具结果。

`status_url` / `result_url` 这类路径**留在**返回值里：它们是需求 §4.6
`TaskRef` 契约的一部分（"长任务返回可查询的地址"），不是 REST 的实现细节。
MCP 形态照常下发，作为"去哪儿查进度"的提示。

## 错误模型

| 情况 | 门面怎么做 | 为什么 |
| --- | --- | --- |
| **业务事实**（附件没了 / 空文件 / 超限 / 类型不符） | **返回** `outcome="blocked"` | 这是业务结论，调用成功了，只是结论是"这单做不下去"。抛异常会迫使每个协议层各自判断一次"这算不算错误" |
| 其余 `AppError`（找不到 / 状态冲突 / 瞬时故障） | **抛出**，原样上抛 | 状态码是**协议**的事：REST 映射成 404/409/503，MCP 映射成 MCP 的错误载荷。门面替它们决定就是越界 |
| 参数不合法（id 不是数字串 / 关注点不是 JSON 数组） | 抛 `ValueError` | 同上，协议层各自翻成 400 |

`outcome` 说的是**这次调用做了什么**（`pulled` / `queued` / `reused` / `blocked`），
与"业务对象现在是什么状态"无关 —— 后者在 `task_ref.status` 或 `write_status` 里。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from types import MappingProxyType
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import Actor, Permission, require
from app.errors import AppError, BUSINESS_FACT_CODES
from app.models import ApprovalTask
from app.ports.approval_gateway import ApprovalReadGateway
from app.ports.object_storage import ObjectStorage
from app.rules.llm_judge import PROMPT_VERSION
from app.schemas import ParseOptions
from app.services.attachment_service import AttachmentService
from app.services.parse_service import request_parse
from app.services.pull_service import ApprovalInboundService
from app.services.result_service import SavedResult
from app.services.result_service import save_review_result as save_review_result_service
from app.services.rule_service import context_for_parse, request_rule_run
from app.services.writeback_service import WritebackRef, request_writeback

#: 需求 2.4.10 的七个工具名，**逐字**。
#:
#: 单独列出来是为了让"恰好七个"成为一个可断言的事实，而不是
#: 靠人数函数。多一个或少一个都会让 `test_m7_contracts.py` 直接失败。
TOOL_NAMES: tuple[str, ...] = (
    "list_pending_contract_approvals",
    "get_contract_approval",
    "download_contract_attachment",
    "parse_contract_document",
    "run_contract_rules",
    "save_review_result",
    "write_approval_comment",
)

#: 工具名 → 所需权限。
#:
#: **两张协议层都从这里取**（REST 的 `Depends(require_permissions(...))` 与
#: 门面自己的 `_require`）。写成两份映射时，改了一处忘了另一处，
#: 结果是"REST 要求 A、MCP 要求 B" —— 而两种形态都不报错，
#: 只是其中一个形态少了一道门。这类漏洞不会有任何症状。
REQUIRED_PERMISSIONS: Mapping[str, Permission] = MappingProxyType(
    {
        "list_pending_contract_approvals": Permission.TASK_READ,
        "get_contract_approval": Permission.TASK_READ,
        "download_contract_attachment": Permission.TASK_READ,
        "parse_contract_document": Permission.REVIEW_EXECUTE,
        "run_contract_rules": Permission.REVIEW_EXECUTE,
        "save_review_result": Permission.RESULT_SAVE,
        "write_approval_comment": Permission.WRITEBACK_EXECUTE,
    }
)


def _require(actor: Actor, tool: str) -> Actor:
    """按 `REQUIRED_PERMISSIONS` 校验权限。

    MCP 形态没有 FastAPI 依赖，这里是它**唯一**的关口；
    REST 形态在依赖里已经查过一次（换来"请求进端点前就被拒"），
    这里是第二次。**刻意不做成"只查一次"**：门面的调用方是两种协议，
    把检查放在任一协议里，另一个就自动失去它。
    """
    return require(actor, REQUIRED_PERMISSIONS[tool])


# ============================================================
# 需求形态 → 本系统形态（唯一的转换点）
# ============================================================

#: 需求里的 id 是十进制数字串。**只认这一种写法**：
#: `" 12"` / `"12.0"` / `"+12"` / `"0x0c"` 都能被某些解析器接受，
#: 于是"该传哪种"变成一个没有正确答案的问题 —— 而它们指向同一个附件时，
#: 两种都接受就意味着两个不同的字符串能拿到同一份结果，缓存与审计都跟着含糊。
_LEGACY_ID_PATTERN = re.compile(r"[0-9]+")


def _legacy_id(value: str, *, parameter: str, canonical: str) -> int:
    """需求形态的 id（十进制字符串）→ 本系统主键（正整数）。

    Raises:
        ValueError: 不是十进制数字串，或小于 1。
            ⚠️ 刻意不用 `PermanentError`：那会让它落进
            "其他确定性失败 → 500"，于是"id 写错了"看起来像"服务端崩了"。
            本仓库对"调用方传来的标识不合法"的约定是 `ValueError` → 400。
    """
    # `isinstance` 先判，不能直接 `re.fullmatch`：传 `12` 时正则抛
    # `TypeError`，而那是**调用方**的错误，不是代码缺陷。
    if isinstance(value, str) and _LEGACY_ID_PATTERN.fullmatch(value):
        number = int(value)
        if number >= 1:
            return number

    raise ValueError(
        f"{parameter} 必须是十进制数字字符串（需求 2.4.10 把它定义为字符串），"
        f"实际收到 {value!r}（{type(value).__name__}）。"
        f"它指向本系统的 {canonical}，是一个正整数。"
    )


def _legacy_focus_points(value: str) -> list[str]:
    """需求形态的关注点（**JSON 字符串**）→ 服务层的 `list[str]`。

    需求把 `focus_points_json` 定义为字符串，服务层要的是列表。
    只在这一处转换，REST 侧由此要 `json.dumps` 一次 —— 一次可见的往返，
    好过让服务层同时接受"列表或字符串"两种形态。

    Raises:
        ValueError: 不是合法 JSON，或不是字符串数组。
    """
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"focus_points_json 必须是 JSON 数组字符串（需求 2.4.10 的定义），"
            f"实际收到 {value!r}（{type(value).__name__}）。"
            f'例如 "[\\"付款条件须与验收挂钩\\"]"。'
        ) from exc

    if not isinstance(parsed, list) or any(
        not isinstance(item, str) for item in parsed
    ):
        raise ValueError(
            f"focus_points_json 必须是**字符串数组**的 JSON，实际解析出 "
            f"{type(parsed).__name__}：{parsed!r}。"
            "允许对象或数字数组会让关注点在下游被当成字符串渲染成 "
            "'[object Object]' —— 而那看起来像模型输出的乱码。"
        )

    return parsed


# ============================================================
# 工具 1：拉取待审批合同
# ============================================================


def list_pending_contract_approvals(
    limit: int = 20,
    *,
    session: Session,
    gateway: ApprovalReadGateway,
    actor: Actor,
) -> dict[str, Any]:
    """拉取待处理审批单并去重入库。

    重复拉取是无害的：已存在的任务只刷新审批单字段，不重建记录、
    不清空解析结果、不重置任务状态。
    """
    _require(actor, "list_pending_contract_approvals")

    result = ApprovalInboundService(gateway, session).list_pending_contract_approvals(
        limit=limit
    )

    return {
        "outcome": "pulled",
        "provider": result.provider,
        "tenant_id": result.tenant_id,
        "fetched": result.fetched,
        "created": result.created,
        "updated": result.updated,
        "items": [
            {
                "task_id": item.task_id,
                "instance_id": item.instance_id,
                "approval_code": item.approval_code,
                "created": item.created,
            }
            for item in result.items
        ],
        "job_idempotency_key": result.job_idempotency_key,
    }


# ============================================================
# 工具 2：查询审批单详情
# ============================================================


def get_contract_approval(
    instance_id: str,
    *,
    session: Session,
    gateway: ApprovalReadGateway,
    actor: Actor,
) -> dict[str, Any]:
    """查询详情，并同步权威上下文、审批表单与附件元数据。

    会写库：详情是权威上下文与附件清单的唯一来源，不落库的话，
    外部系统不可用或实例被删除后就什么都看不到了。
    """
    _require(actor, "get_contract_approval")

    result = ApprovalInboundService(gateway, session).get_contract_approval(instance_id)

    return {
        "outcome": "synced",
        "task_id": result.task_id,
        "instance_id": result.instance_id,
        "approval_code": result.approval_code,
        "task_status": result.task_status,
        "context_status": result.context_status,
        "our_party_name": result.our_party_name,
        "our_party_contract_label": result.our_party_contract_label,
        "our_party_business_role": result.our_party_business_role,
        "contract_type": result.contract_type,
        "form_data": result.form_data,
        "attachments": [asdict(item) for item in result.attachments],
    }


# ============================================================
# 工具 3：下载合同附件
# ============================================================


def download_contract_attachment(
    instance_id: str,
    attachment_id: str,
    file_name: str | None = None,
    *,
    session: Session,
    gateway: ApprovalReadGateway,
    storage: ObjectStorage,
    actor: Actor,
) -> dict[str, Any]:
    """下载附件并落盘：校验类型与大小 → SHA-256 → 对象存储 → 受控物化路径。

    返回的 `file_path` 是**受控临时物化路径**（相对路径，不含服务器绝对目录），
    供后续解析与调用方读取。对象键（长期保存位置）**不下发** ——
    它属内部存储布局，下发会让 M9 换 MinIO 变成破坏性变更。
    """
    _require(actor, "download_contract_attachment")

    service = AttachmentService(gateway, storage, session)

    try:
        result = service.download_contract_attachment(
            instance_id, attachment_id, file_name
        )
    except AppError as exc:
        # 业务事实（附件已被删除 / 空文件 / 超限 / 类型不符）→ 业务结论；
        # 其余原样上抛，由各协议层翻译成自己的错误形态。
        if exc.code in BUSINESS_FACT_CODES:
            return _blocked_outcome(session, gateway, instance_id, attachment_id, exc)
        raise

    return {
        "outcome": "downloaded",
        "task_id": result.task_id,
        "attachment_id": result.attachment_id,
        "file_name": result.file_name,
        "file_size": result.file_size,
        "file_checksum": result.file_checksum,
        "file_path": result.file_path,
        "content_type": result.content_type,
        "download_status": result.download_status,
    }


# ============================================================
# 工具 4：解析合同文档（**异步**）
# ============================================================


def parse_contract_document(
    document_id: str,
    *,
    session: Session,
    engine_version: str,
    actor: Actor,
    parse_options: ParseOptions | None = None,
) -> dict[str, Any]:
    """入队一次解析，返回可查询的作业引用。

    重复调用无害：同一附件 + 同一解析器版本 + 同一配置命中缓存，
    不新建作业、不重复跑 OCR。上一次解析是失败或阻塞时会真正重跑。

    ⚠️ `parse_options` 是**企业上下文**（关键字专属），不是需求的最低参数：
    它进 `config_digest`，进而进 `cache_key`。把它丢掉不会报错，
    只会让调用方传的参数**静默失效**、并让两次不同配置的解析互相命中缓存 ——
    因此它必须原样透传，不能用默认值顶替。
    """
    _require(actor, "parse_contract_document")

    attachment_id = _legacy_id(
        document_id,
        parameter="document_id",
        canonical="approval_attachments.id（附件主键）",
    )

    try:
        result = request_parse(
            session,
            document_id=attachment_id,
            options=parse_options,
            engine_version=engine_version,
        )
    except AppError as exc:
        # 业务事实（附件缺失 / 尚未下载 / 记录不存在）→ 业务结论
        if exc.code in BUSINESS_FACT_CODES:
            return _parse_blocked_outcome(document_id, exc)
        raise

    return {
        "outcome": "queued",
        # §4.6 的 `TaskRef`：**必须带 `job_id`** —— 一个任务会产生多个作业
        # （多附件、多次重试），只用 `task_id` 答不了"我刚提交的那次怎么样了"。
        "task_ref": {
            "job_id": result.job_id,
            "task_id": result.task_id,
            "status": result.job_status,
            "status_url": f"/api/jobs/{result.job_id}",
        },
        "result_url": f"/api/parses/{result.parse_id}",
        "cache_hit": result.cache_hit,
    }


# ============================================================
# 工具 5：执行合同规则审查（**异步**）
# ============================================================


def run_contract_rules(
    case_id: str,
    *,
    session: Session,
    actor: Actor,
    force: bool = False,
) -> dict[str, Any]:
    """入队一次规则审查，返回可查询的批次引用。

    ⚠️ `case_id` 在这里是**解析编号**（`contract_parses.id`），
    不是工具 6 里那个同名参数（那是批次编号）—— 理由见模块 docstring。
    调用时批次还不存在，本函数正是创建它的那个。
    """
    _require(actor, "run_contract_rules")

    parse_id = _legacy_id(
        case_id,
        parameter="case_id",
        canonical="contract_parses.id（本次审查所依据的解析）",
    )

    context = context_for_parse(session, parse_id)
    start = request_rule_run(
        session,
        parse_id=parse_id,
        context=context,
        prompt_version=PROMPT_VERSION,
        force=force,
    )

    return {
        # `outcome` 说的是**这次调用做了什么**：新建批次与作业、还是命中既有批次。
        # ⚠️ 它与"跑完了没有"无关 —— 那是 `task_ref.status`（作业状态）的事。
        "outcome": "reused" if start.reused else "queued",
        # §4.6 的 `TaskRef`。
        # ⚠️ **`job_id` 必须有**：调用方轮询的是**作业**（`/api/jobs/{job_id}`），
        # 而一个批次可能有多个作业（重跑、租约回收后重派）。
        # 只给 `run_id` 的话，轮询只能打到批次上 —— 而批次一建出来就是 `running`，
        # 没有"排队中"这个状态，于是"入队了没"这件事在接口上根本无法回答。
        "task_ref": {
            "job_id": start.job_id,
            "run_id": start.run_id,
            "version_no": start.version_no,
            # ⚠️ **照实读回作业状态**（`WorkflowJob.job_status`），不从 `reused` 推断：
            # 六项全同但上一次仍在跑时 `reused=True`，把它答成"已完成"会让调用方
            # 停止轮询，然后去读一个**还没有结论**的批次。
            "status": start.job_status,
            "status_url": f"/api/jobs/{start.job_id}",
        },
        "result_url": f"/api/runs/{start.run_id}",
        "cache_hit": start.reused,
    }


# ============================================================
# 工具 6：保存审查结果（**同步**）
# ============================================================


def save_review_result(
    case_id: str,
    overall_risk_level: str,
    summary_text: str,
    focus_points_json: str,
    comment_text: str,
    *,
    session: Session,
    actor: Actor,
) -> dict[str, Any]:
    """把一份审查结果落进 `review_results`，同步返回保存结果。

    ⚠️ `case_id` 在这里是**批次编号**（`review_runs.id`），
    与工具 5 的同名参数指向不同对象（见模块 docstring）。

    一次插入、没有外部调用，因此不做成作业：调用方拿到 200 时结果**已经在库里**。
    `overall_risk_level` 由调用方传入但与聚合不符时会被拒
    （`RESULT_INPUT_MISMATCH`）—— 拿旧批次的风险等级保存新结果，
    会把两份口径焊进同一条记录，而那条记录看起来完全正常。
    """
    _require(actor, "save_review_result")

    run_id = _legacy_id(
        case_id,
        parameter="case_id",
        canonical="review_runs.id（审查批次）",
    )
    focus_points = _legacy_focus_points(focus_points_json)

    saved: SavedResult = save_review_result_service(
        session,
        run_id=run_id,
        overall_risk_level=overall_risk_level,
        summary_text=summary_text,
        focus_points_json=focus_points,
        comment_text=comment_text,
        actor=actor,
    )

    return {
        # `outcome` 说的是**这次调用做了什么**：新建了一版，还是复用了既有版本。
        "outcome": "reused" if saved.reused else "saved",
        "result_id": saved.result_id,
        "run_id": saved.run_id,
        "task_id": saved.task_id,
        "version_no": saved.version_no,
        # 照实读回库里的等级（而不是回显入参）：两者相等是服务层校验过的**事实**，
        # 而不是一个"我们以为我们写进去了"的假设。
        "overall_risk_level": saved.overall_risk_level,
        # 回写正文的摘要 —— 人工确认绑定在它上面，调用方可据此确认
        # "我确认的是不是这一份"。它不是机密（正文才是），可以下发。
        "content_digest": saved.content_digest,
        "result_url": f"/api/results/{saved.result_id}",
    }


# ============================================================
# 工具 7：回写审批意见（**同步返回 + 后台派发**）
# ============================================================


def write_approval_comment(
    instance_id: str,
    review_id: str,
    *,
    session: Session,
    actor: Actor,
) -> dict[str, Any]:
    """把结果正文登记为一次回写意图，返回可轮询的回写引用。

    ⚠️ **返回时评论还没写出去。** 外部调用不可与本地事务原子提交，
    因此这里只在一个事务里写 `comment_logs(writing)` + `outbox_events(pending)`
    + 审计事件，送达交给独立的 Outbox 派发器。

    ⚠️ 正文与幂等键**都不是入参**：正文取自已保存的结果，
    幂等键由服务端按 `(provider, tenant, instance, result_id, content_digest)` 算出。
    """
    _require(actor, "write_approval_comment")

    result_id = _legacy_id(
        review_id,
        parameter="review_id",
        canonical="review_results.id（审查结果）",
    )

    ref: WritebackRef = request_writeback(
        session,
        instance_id=instance_id,
        result_id=result_id,
        actor=actor,
    )

    return {
        # 门禁拒绝是业务结论 → `blocked`，而不是错误。
        # 拒绝对应的是"等人确认"这类需要**人**介入的状态，
        # 用错误码会让调用端当成故障反复重试一个注定被拒的请求，
        # 而它等的那个确认永远不会因为重试而出现。
        "outcome": "blocked" if ref.reason_code is not None else "accepted",
        "reused": ref.reused,
        "writeback_ref": writeback_ref_json(ref),
    }


def writeback_ref_json(ref: WritebackRef) -> dict[str, Any]:
    """`WritebackRef` → JSON（工具 7 与查询接口共用同一形状）。

    拒绝时 `attempt_id` 仍非空 —— 拒绝也是**一次尝试**，也留下了证据行。
    把它置空会让"被拒绝过"这件事在接口上消失，而库里明明记着。
    """
    return {
        "attempt_id": ref.attempt_id,
        "task_id": ref.task_id,
        "instance_id": ref.instance_id,
        "result_id": ref.result_id,
        "write_status": ref.write_status,
        # 程序判据用 reason_code，文案用 reason_text；两者都给，
        # 让调用方既能分支又能直接展示。
        "reason_code": ref.reason_code,
        "reason_text": ref.reason_text,
        "reused": ref.reused,
        "outbox_event_id": ref.outbox_event_id,
        "status_url": (
            None if ref.attempt_id is None else f"/api/writebacks/{ref.attempt_id}"
        ),
    }


# ============================================================
# 业务事实的两种形态
# ============================================================


def _parse_blocked_outcome(document_id: str, error: AppError) -> dict[str, Any]:
    """工具 4 的"业务事实"失败 → 业务结论。"""
    return {
        "outcome": "blocked",
        "error_code": str(error.code),
        "message": error.message,
        "retryable": False,
        "document_id": document_id,
        "task_ref": None,
        "result_url": None,
    }


def _blocked_outcome(
    session: Session,
    gateway: ApprovalReadGateway,
    instance_id: str,
    attachment_id: str,
    error: AppError,
) -> dict[str, Any]:
    """工具 3 的"业务事实"失败 → 业务结论。

    ⚠️ 任务状态是**查出来的真实状态**，不是从异常推断的 ——
    推断会让"服务其实没阻塞任务"这类缺陷永远藏在"看起来很对"的响应里。
    """
    task = session.execute(
        select(ApprovalTask).where(
            ApprovalTask.provider == gateway.provider,
            ApprovalTask.tenant_id == gateway.tenant_id,
            ApprovalTask.instance_id == instance_id,
        )
    ).scalar_one_or_none()

    return {
        "outcome": "blocked",
        "error_code": str(error.code),
        "message": error.message,
        "retryable": False,
        "instance_id": instance_id,
        "attachment_id": attachment_id,
        "task_id": None if task is None else task.id,
        "task_status": None if task is None else task.task_status,
        "blocked_stage": None if task is None else task.blocked_stage,
        "block_reason": None if task is None else task.block_reason,
    }
