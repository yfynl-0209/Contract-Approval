"""工具的 REST 入口（M3 交付工具 1–5，M6 交付工具 6–7，M7 收敛到门面）。

本模块刻意"薄"：只把 HTTP 请求翻译成**门面调用**、把结果翻译回 JSON，
没有 `if`、没有业务分支。这是"REST 与 MCP 共用一套业务逻辑"能否成立的**唯一**前提 ——
一旦这里出现第一个业务判断，M7 的 MCP 形态要么复制它，要么依赖它。

    HTTP 请求 ──→ 本模块（形态转换）──→ app/tool_facade.py（编排）──→ app/services/*
    MCP 调用  ──→ app/mcp_server.py ──┘

## 本模块只剩三类**形态**转换

| 转换 | 例子 | 为什么留在这一层 |
| --- | --- | --- |
| 请求体 → 门面参数 | `payload.run_id` → `"12"` | 需求把 id 定义为字符串，Pydantic 帮我们验成 `int`；回写成字符串是**协议形态**，不是业务 |
| 业务结论 → HTTP | `AppError` → 404/409/503 | 状态码是 HTTP 的词汇，门面不该知道 |
| 依赖注入 → 显式参数 | `Depends(get_db)` → `session=` | 同上 |

⚠️ **旧参数名（`case_id` / `review_id`）在本层被拒。** 请求体用
`extra="forbid"`，传旧名字得到 422 与一条说清"该传什么"的消息 ——
兼容名只在门面上存在一处（见 `tests/test_m6_api.py` 的同名断言）。

## 同步 / 异步的分界（企业化设计 §6）

| 工具 | 形态 | 理由 |
| --- | --- | --- |
| 4 解析 / 5 规则 | **异步**，返回 `task_ref` | 长任务（OCR 数秒、9 条规则走模型），同步会让调用方挂着连接 |
| 6 保存结果 | **同步**，直接返回结果 | 一次插入，无外部调用 |
| 7 回写 | **同步返回 + 后台派发** | 登记意图是一次插入；**外部调用**交给 Outbox 派发器 |

⚠️ 工具 7 的"异步"与 4/5 的**不同**：4/5 返回的是**作业引用**（活儿还没干），
工具 7 返回的是**已登记的意图**（本地事实已经成立，剩下的是送达）。
因此它给的是可轮询的 `writeback_ref`，而不是 `task_ref`。
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import tool_facade
from app.api.deps import (
    get_db,
    get_gateway,
    get_parser_engine_version,
    get_storage,
    require_permissions,
)
from app.auth import Actor, Permission
from app.ports.approval_gateway import ApprovalReadGateway
from app.ports.object_storage import ObjectStorage
from app.schemas import (
    DownloadAttachmentRequest,
    GetApprovalRequest,
    ListPendingRequest,
    ParseContractDocumentRequest,
    RunContractRulesRequest,
    SaveReviewResultRequest,
    WriteApprovalCommentRequest,
)

router = APIRouter(prefix="/tools", tags=["工具（REST 形态）"])


# ============================================================
# 工具 1：拉取待审批合同
# ============================================================


@router.post(
    "/list_pending_contract_approvals",
    summary="工具 1：拉取待审批合同",
    description=(
        "拉取待处理审批单并按 `(provider, tenant_id, instance_id)` 去重入库。\n\n"
        "**重复拉取是无害的**：已存在的任务只刷新审批单本身的字段，"
        "不重建记录、不清空解析结果、不重置任务状态。\n\n"
        "⚠️ 刚拉取完的任务 `context_status` 必然是 `missing` —— "
        "待办列表接口不含权威审查上下文（我方立场），那是详情接口才有的信息。"
    ),
)
def list_pending_contract_approvals(
    payload: ListPendingRequest,
    session: Session = Depends(get_db),
    gateway: ApprovalReadGateway = Depends(get_gateway),
    actor: Actor = Depends(require_permissions(Permission.TASK_READ)),
) -> dict[str, Any]:
    return tool_facade.list_pending_contract_approvals(
        payload.limit, session=session, gateway=gateway, actor=actor
    )


# ============================================================
# 工具 2：查询审批单详情
# ============================================================


@router.post(
    "/get_contract_approval",
    summary="工具 2：查询审批单详情",
    description=(
        "查询审批单详情，并同步**权威审查上下文**、审批表单与附件元数据。\n\n"
        "本接口**会写库**：详情是权威上下文与附件清单的唯一来源，"
        "不落库的话，外部系统不可用或实例被删除后就什么都看不到了。\n\n"
        "任务不存在时会自动创建 —— 详情携带了建任务所需的全部基础字段，"
        "因此「没拉取先查详情」是正常用法。\n\n"
        "⚠️ 返回体**不含**附件字节内容；下载走工具 3。"
    ),
)
def get_contract_approval(
    payload: GetApprovalRequest,
    session: Session = Depends(get_db),
    gateway: ApprovalReadGateway = Depends(get_gateway),
    actor: Actor = Depends(require_permissions(Permission.TASK_READ)),
) -> dict[str, Any]:
    return tool_facade.get_contract_approval(
        payload.instance_id, session=session, gateway=gateway, actor=actor
    )


# ============================================================
# 工具 3：下载合同附件
# ============================================================


@router.post(
    "/download_contract_attachment",
    summary="工具 3：下载合同附件",
    description=(
        "下载附件并落盘：校验类型与大小 → SHA-256 → 对象存储 → 受控物化路径。\n\n"
        "**返回 `file_path`：受控临时物化路径。**\n\n"
        "- 它是需求 2.4.4 要求的「本地文件路径」，供后续解析工具与调用方读取；\n"
        "- 它是**相对路径**（`workspace/…`），不含服务器绝对目录，"
        "也**不得**被当成可绕过鉴权的永久公开地址下发。\n\n"
        "⚠️ **长期保存位置（对象键）不在本响应中**。它属于内部实现细节："
        "下发它会让调用方依赖具体的存储布局（本地文件 / MinIO），"
        "从而把 M9 的实现替换变成**破坏性变更**。需要时从任务查询接口获取。\n\n"
        "当附件本身有问题（已被删除 / 空文件 / 超限 / 类型不符）时，"
        "返回 **HTTP 200** 且 `outcome = \"blocked\"`：\n"
        "这是一个需要人工处理的**业务结论**，不是系统故障。"
    ),
)
def download_contract_attachment(
    payload: DownloadAttachmentRequest,
    session: Session = Depends(get_db),
    gateway: ApprovalReadGateway = Depends(get_gateway),
    storage: ObjectStorage = Depends(get_storage),
    actor: Actor = Depends(require_permissions(Permission.TASK_READ)),
) -> dict[str, Any]:
    return tool_facade.download_contract_attachment(
        payload.instance_id,
        payload.attachment_id,
        payload.file_name,
        session=session,
        gateway=gateway,
        storage=storage,
        actor=actor,
    )


# ============================================================
# 工具 4：解析合同文档（**异步**）
# ============================================================


@router.post(
    "/parse_contract_document",
    summary="工具 4：解析合同文档",
    description=(
        "**异步**入队一次解析，立即返回可查询的作业引用。\n\n"
        "解析是**长任务**（OCR 一页扫描件要数秒），同步返回会让调用方一直挂着连接。\n"
        "因此本接口只做三件事：预留解析占位 → 创建解析作业 → 返回 `task_ref`。\n\n"
        "**轮询方式**：`GET /api/jobs/{job_id}`；成功时它带 `result_ref`，\n"
        "指向 `GET /api/parses/{parse_id}`（结构化字段就在那里）。\n\n"
        "**重复调用是无害的**：同一份附件 + 同一解析器版本 + 同一配置会命中缓存 ——\n"
        "**不新建作业、不重复跑 OCR**，`cache_hit=true` 说明发生了这件事。\n"
        "若上一次解析是**失败或阻塞**的，它会真正重跑（失败记录不构成缓存命中）。\n\n"
        "⚠️ 需要附件**已经下载成功**（先调工具 3）：没下载就没有字节可解析，\n"
        "本接口会返回 `blocked` 而不是入队后让作业反复失败。"
    ),
)
def parse_contract_document(
    payload: ParseContractDocumentRequest,
    session: Session = Depends(get_db),
    engine_version: str = Depends(get_parser_engine_version),
    actor: Actor = Depends(require_permissions(Permission.REVIEW_EXECUTE)),
) -> dict[str, Any]:
    # `str(...)` 是**形态**转换：需求把 `document_id` 定义为字符串，
    # 而请求体里它是 `int`（`ge=1`）。回写成字符串是为了让门面的签名
    # 与需求逐字一致 —— 那次往返是可见的一行，而不是一个隐式的别名。
    return tool_facade.parse_contract_document(
        str(payload.document_id),
        session=session,
        engine_version=engine_version,
        actor=actor,
        parse_options=payload.parse_options,
    )


# ============================================================
# 工具 5：执行合同规则审查（**异步**）
# ============================================================


@router.post(
    "/run_contract_rules",
    summary="工具 5：执行合同规则审查",
    description=(
        "**异步**入队一次规则审查，立即返回可查询的批次引用。\n\n"
        "40 条规则里有 9 条要走模型，同步返回会让调用方一直挂着连接；"
        "因此本接口只做三件事：读权威上下文 → 建批次 → 建作业。\n\n"
        "**轮询方式**：`GET /api/jobs/{job_id}`；成功时它带 `result_ref`，"
        "指向 `GET /api/runs/{run_id}`（**现算的**总风险 / 结论完整性 / 四态计数 / 关注点）。\n\n"
        "**默认复用**：六项输入（解析版本 / 上下文快照 / 规则集 / 模型 / 提示词 / 配置）"
        "全同则复用既有批次，不重跑、不覆盖历史。要强制重跑传 `force=true` ——\n"
        "含 `needs_review` 的批次需要重跑时用它，而不是「改个不相关的参数去骗过缓存」。\n\n"
        "⚠️ 我方立场**不是入参**：它取自审批记录的既有事实并冻结进批次快照。\n"
        "⚠️ `parse_id` 必须已通过质量门禁（`succeeded`）—— 在残缺的输入上跑规则，"
        "得到的是一份「看起来正常」的报告。"
    ),
)
def run_contract_rules(
    payload: RunContractRulesRequest,
    session: Session = Depends(get_db),
    actor: Actor = Depends(require_permissions(Permission.REVIEW_EXECUTE)),
) -> dict[str, Any]:
    return tool_facade.run_contract_rules(
        str(payload.parse_id), session=session, actor=actor, force=payload.force
    )


# ============================================================
# 工具 6：保存审查结果（**同步**）
# ============================================================


@router.post(
    "/save_review_result",
    summary="工具 6：保存审查结果",
    description=(
        "把一份审查结果落进 `review_results`，**同步返回保存结果**。\n\n"
        "一次插入、没有外部调用，因此不做成作业（企业化设计 §6 的同步/异步分界）。\n"
        "调用方拿到 200 时结果**已经在库里**，`result_url` 立刻可读 ——\n"
        "返回 `task_ref` 会让一次插入变成一次轮询。\n\n"
        "**口径必须与批次聚合一致**：`overall_risk_level` 由调用方传入，"
        "但与 M5 聚合不符时会被拒（`RESULT_INPUT_MISMATCH`）——\n"
        "拿旧批次的风险等级保存新结果，会把两份口径焊进同一条记录，"
        "而那条记录看起来完全正常。\n\n"
        "**重复保存是无害的**：同批次 + 内容与输入完全一致 → 命中复用，"
        "`outcome=reused` 且**不新建版本**。内容或输入变化 → 新版本，"
        "旧版本保留并由 `supersedes_result_id` 串成版本链。\n\n"
        "⚠️ 版本一旦新建，**旧版本上的人工确认立即失效**"
        "（确认绑定的是当时那份正文，而当前版本已经不是它）。\n\n"
        "**错误语义**：批次不存在 → 404；批次未完成 → **409**（等它跑完再来）；"
        "风险等级不符 → 400。三者都是**稳定机器码**，不是 500。"
    ),
)
def save_review_result(
    payload: SaveReviewResultRequest,
    session: Session = Depends(get_db),
    actor: Actor = Depends(require_permissions(Permission.RESULT_SAVE)),
) -> dict[str, Any]:
    # 请求体里关注点是列表，门面收的是需求定义的 JSON 字符串。
    return tool_facade.save_review_result(
        str(payload.run_id),
        payload.overall_risk_level,
        payload.summary_text,
        json.dumps(payload.focus_points, ensure_ascii=False),
        payload.comment_text,
        session=session,
        actor=actor,
    )


# ============================================================
# 工具 7：回写审批意见（**同步返回 + 后台派发**）
# ============================================================


@router.post(
    "/write_approval_comment",
    summary="工具 7：回写审批意见",
    description=(
        "把结果正文**登记**为一次回写意图，返回可轮询的 `writeback_ref`。\n\n"
        "⚠️ **本接口返回时评论还没写出去**。外部调用不可与本地事务原子提交，"
        "因此这里只做一件事：在一个事务里写 `comment_logs(writing)` +\n"
        "`outbox_events(pending)` + 审计事件，送达交给独立的 Outbox 派发器。\n"
        "轮询方式：`GET /api/writebacks/{attempt_id}`。\n\n"
        "| `write_status` | 含义 |\n"
        "| --- | --- |\n"
        "| `writing` | 意图已登记，等待派发 |\n"
        "| `success` | 已送达外部系统 |\n"
        "| `failed` | 外部失败或重试耗尽，任务进入 `blocked`（恢复点是**回写**，不是重审） |\n"
        "| `not_written` | 门禁拒绝，**没有发起回写**（见下） |\n\n"
        "**门禁拒绝是业务结论 → HTTP 200 + `outcome=\"blocked\"`**，"
        "而不是 5xx：拒绝对应的是「等人确认」这类需要**人**介入的状态，"
        "用 5xx 会让调用端当成抖动反复重试一个注定被拒的请求，"
        "而它等的那个确认永远不会因为重试而出现。\n"
        "拒绝原因在 `writeback_ref.reason_code`（稳定机器码）：\n"
        "未人工确认 / 高风险未确认 / 待人工判断未确认 / 立场上下文不可信 /"
        "正文为空 / 已回写过 / 结果与实例不匹配。\n\n"
        "**重复调用是无害的**：同一个结果 + 同一份正文 → 返回**同一次**尝试"
        "（`reused=true`）。正文变了（新版本）则是**另一次**回写，不是重放。\n\n"
        "⚠️ 正文与幂等键**都不是入参**：正文取自已保存的结果，"
        "幂等键由服务端按 `(provider, tenant, instance, result_id, content_digest)` 算出。"
    ),
)
def write_approval_comment(
    payload: WriteApprovalCommentRequest,
    session: Session = Depends(get_db),
    actor: Actor = Depends(require_permissions(Permission.WRITEBACK_EXECUTE)),
) -> dict[str, Any]:
    return tool_facade.write_approval_comment(
        payload.instance_id, str(payload.result_id), session=session, actor=actor
    )
