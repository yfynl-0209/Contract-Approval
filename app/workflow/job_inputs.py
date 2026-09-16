"""各 `job_type` 的**严格输入模型**（设计文档 §4.5）。

## 为什么输入必须建模，而不是收任意 JSON

作业输入由外部调用方（工具 4）或上游服务生成，**键名拼错**是常见错误。
宽松解析会让 `sourc_checksum` 这类笔误被静默丢弃：作业照常入队、照常"成功"，
只是用默认值跑了一遍 —— 调用方以为参数生效了。

所以每种 `job_type` 一个 `StrictModel`（`extra="forbid"` + `frozen=True`），
与 M3 的 `ToolRequest` 同一套做法。

## 与 `idempotency_key` 的分工

业务输入**必须**写进 `input_json`，**不得**只塞进 `idempotency_key`，也不得从键里反解。
键是**摘要**，为去重而设计；一旦有人依赖"从键里能读出参数"，改键格式就成了
破坏性变更，而且破坏方式是"读出来的值变了，不报错"。
"""

from __future__ import annotations

from pydantic import Field

from app.auth import Actor
from app.enums import JobType

#: `ParseOptions` 定义在 `app/schemas.py`（工具 4 的请求体也要用它），
#: 这里**转出**以保持既有导入路径可用。
#:
#: ⚠️ 它**不能**定义在本模块：本模块依赖 `app.schemas.StrictModel`，
#: 而 `app.schemas` 的 `ParseContractDocumentRequest` 需要 `ParseOptions` ——
#: 定义在这里就构成**循环导入**。依赖方向必须是单向的
#: `workflow → schemas`，而 `ParseOptions` 属于"输入校验"这一类，本就该在 `schemas`。
from app.schemas import ParseOptions as ParseOptions  # noqa: PLC0414  (显式转出)
from app.schemas import StrictModel


class PullJobInput(StrictModel):
    """拉取是**批量**操作，因此输入只描述租户范围，不含单个实例。"""

    provider: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)


class DetailJobInput(StrictModel):
    provider: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    instance_id: str = Field(min_length=1)


class DownloadJobInput(StrictModel):
    provider: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    instance_id: str = Field(min_length=1)
    #: 外部审批系统的附件编号
    attachment_id: str = Field(min_length=1)
    #: 本系统 `approval_attachments.id`。⚠️ 可为空：**首次下载时附件记录还不存在**
    #: （记录在下载成功后才建）。填一个"事后才成立"的编号会让
    #: "这份结果基于什么输入" 记录下一个当时不存在的值。
    attachment_record_id: int | None = Field(default=None, ge=1)


class ParseJobInput(StrictModel):
    """解析作业的输入。

    `parse_id` 必须在输入里：§3.2 规定**占位行先于作业创建**（同一事务内预留），
    Worker 的职责是**填充那一行**而不是新建一行。缺了它，Worker 只能退化成
    用 `(attachment_id, cache_key)` 反查，而且 `result_ref.parse_id` 也失去稳定来源。
    """

    parse_id: int = Field(ge=1)
    #: 本系统主键，不是外部编号 —— 两者都叫 "attachment id"，见 §4.5 的歧义说明
    attachment_record_id: int = Field(ge=1)
    source_checksum: str = Field(min_length=1)
    object_key: str = Field(min_length=1)
    content_type: str = Field(min_length=1)
    parse_options: ParseOptions = Field(default_factory=ParseOptions)


class ActorPayload(StrictModel):
    """冻结在作业输入里的**发起人身份**（M7）。

    ## 为什么存**结构**而不是一个名字字符串

    作业是"过去那一刻的意图"，Worker 执行时进程身份与发起人无关，
    因此身份必须随输入一起冻结。只存名字时，审计里 `actor_id` 永远是空 ——
    而"张伟"在两个部门各有一个，事后无法区分是谁发起的。

    ⚠️ 这里存的是**已认证主体**，不是调用方自己填的字段：
    作业输入由 API 层用 `Depends(require_permissions(...))` 解出的 `Actor` 写入，
    调用方**没有**提供 `actor` 的入口（见 `app/schemas.py` 的请求模型）。
    """

    actor_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    #: 令牌里原样的角色声明（未识别的不在 `Actor.permissions` 里体现，但保留原话）
    roles: tuple[str, ...] = ()
    tenant_id: str = Field(min_length=1)

    @classmethod
    def of(cls, actor: Actor) -> ActorPayload:
        """从已认证主体生成作业输入片段。"""
        return cls(
            actor_id=actor.actor_id,
            display_name=actor.display_name,
            roles=tuple(sorted(actor.roles)),
            tenant_id=actor.tenant_id,
        )

    def to_actor(self) -> Actor:
        """还原成 `Actor`（Worker 侧使用）。"""
        return Actor(
            actor_id=self.actor_id,
            display_name=self.display_name,
            roles=frozenset(self.roles),
            tenant_id=self.tenant_id,
        )


class ResultJobInput(StrictModel):
    """结果保存作业的输入（工具 6 / `save_review_result`，需求 2.4.10）。

    `overall_risk_level` 由调用方传入但**必须**与 M5 聚合一致 ——
    服务层校验不等时返回稳定的 `RESULT_INPUT_MISMATCH`：
    调用方拿着旧批次的风险等级保存新结果，会把两份口径焊在一条记录上。
    """

    run_id: int = Field(ge=1)
    overall_risk_level: str = Field(min_length=1)  # RiskLevel，服务层与聚合比对
    summary_text: str = Field(min_length=1)
    focus_points_json: list[str] = Field(default_factory=list)
    comment_text: str = Field(min_length=1)
    actor: ActorPayload


class WritebackJobInput(StrictModel):
    """回写作业的输入（工具 7 / `write_approval_comment`，需求 2.4.10）。

    只有"回写哪个结果"与"谁发起"，正文取自结果本身 ——
    留白正文引用能让派发器还原"当时要写什么"，不受后续版本影响。
    """

    instance_id: str = Field(min_length=1)
    result_id: int = Field(ge=1)
    actor: ActorPayload


#: `job_type` → 输入模型。**新增作业类型必须同时登记**，否则 `create_job` 会拒绝它。
INPUT_MODELS: dict[JobType, type[StrictModel]] = {
    JobType.PULL: PullJobInput,
    JobType.DETAIL: DetailJobInput,
    JobType.DOWNLOAD: DownloadJobInput,
    JobType.PARSE: ParseJobInput,
    JobType.RESULT: ResultJobInput,
    JobType.WRITEBACK: WritebackJobInput,
}

#: 尚未定义的作业类型（M7 起补齐）。它们暂不做严格校验，但**必须显式列出**，
#: 而不是靠"没找到模型就放行" —— 那样新增类型会静默失去校验。
PENDING_JOB_TYPES: frozenset[JobType] = frozenset({JobType.RULE})


class JobInputError(ValueError):
    """作业输入非法（缺字段、未知字段、取值越界）。"""


def validate_job_input(job_type: JobType, payload: object) -> dict:
    """按作业类型校验输入，返回**可直接落库**的规范化字典。

    返回的是 `model_dump(mode="json")` 的结果，即**含默认值**的完整输入 ——
    让"这份结果基于什么输入"没有留白：不写默认值的话，读回时无法区分
    "当时用的是默认 DPI" 与 "当时没记录 DPI"。

    Raises:
        JobInputError: 输入为空、含未知字段、缺必填字段或取值越界。
    """
    if not payload:
        raise JobInputError("作业输入不能为空")

    model = INPUT_MODELS.get(job_type)
    if model is None:
        if job_type in PENDING_JOB_TYPES:
            # 显式登记的"待补"类型：原样落库，但这是**有意的放行**，不是兜底
            return dict(payload)  # type: ignore[arg-type]
        raise JobInputError(
            f"作业类型 {job_type!r} 没有登记输入模型，"
            "请在 app/workflow/job_inputs.py 的 INPUT_MODELS 中补齐"
        )

    try:
        return model.model_validate(payload).model_dump(mode="json")
    except Exception as exc:  # noqa: BLE001 - 统一转成调用方可读的异常
        raise JobInputError(f"{job_type.value} 作业输入非法：{exc}") from exc
