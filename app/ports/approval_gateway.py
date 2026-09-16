"""审批系统对接端口：读取能力与评论能力**分开定义**。

真实企业审批系统常常只授予读取权限（回写走 Webhook 或人工通道）。若把读写塞进同一个
Protocol，这类实现就被迫伪造一个永远返回空的评论查询能力 —— 比直接缺失更糟，
因为它看起来是能用的。消费方只依赖真正用到的那一个窄接口：

    ApprovalInboundService / AttachmentService  → ApprovalReadGateway     (M3)
    WritebackService                            → ApprovalCommentGateway  (M6)

DTO 是**内部标准化结构**：厂商原始字段（钉钉的 `process_instance_id` 之类）
不外泄给解析与规则模块；字段映射、分页、限流、鉴权、错误码转换全由 Adapter 承担。

⚠️ 本模块只定义 `Protocol` 与 DTO，**不含任何实现**（实现见 `app/adapters/approval/`）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from app.enums import WriteStatus

# ============================================================
# DTO
# ============================================================


@dataclass(frozen=True)
class PendingApprovalDTO:
    """待办列表项 —— 内部标准化结构。

    **不含权威审查上下文**（业务事实只在详情接口里）。由此推出一个容易被误判为 bug
    的现象：**刚拉取完的任务 `context_status` 必然是 `missing`** —— 这是正确行为。

    `provider` / `tenant_id` 由 Adapter 按自身身份注入，不是外部系统返回的字段。
    """

    provider: str
    tenant_id: str
    instance_id: str
    approval_code: str
    approval_title: str
    applicant_name: str
    apply_time: str
    attachment_count: int


@dataclass(frozen=True)
class AuthoritativeContextDTO:
    """权威审查上下文（**业务事实**，不是解析推断）。

    字段都可能为 `None`：真实审批系统未必提供全部四项。缺失时的正确处理是
    "依赖它的规则判 `needs_review`"，**不是**"任务 `blocked`" ——
    否则在拿不到这些字段的环境里系统会完全不可用（那是自伤，不是安全）。

    ⚠️ 解析模块**永远不能写入**这些值，只能读取并做交叉核验。
    """

    our_party_name: str | None = None
    our_party_contract_label: str | None = None
    our_party_business_role: str | None = None
    contract_type: str | None = None


@dataclass(frozen=True)
class AttachmentDTO:
    """附件元数据。"""

    attachment_id: str
    file_name: str
    file_type: str
    #: 外部系统标记该附件是否仍可下载；False 时下载必然失败（缺失 → blocked → 人工重试）
    available: bool = True


@dataclass(frozen=True)
class ApprovalDetailDTO:
    """审批单详情：审批信息 + 表单 + 附件清单 + 权威审查上下文。

    `form_data` 原样落库，外部系统不可用或实例被删时历史详情仍可查看。
    ⚠️ 其中的人员姓名、证件号、联系方式**禁止进入日志**。
    """

    instance_id: str
    approval_code: str
    approval_title: str
    applicant_name: str
    apply_time: str
    context: AuthoritativeContextDTO
    form_data: dict[str, Any] = field(default_factory=dict)
    attachments: tuple[AttachmentDTO, ...] = ()


@dataclass(frozen=True)
class DownloadedAttachmentDTO:
    """下载到的附件字节流。

    `content_type` 取自**响应头**而不是文件名后缀：后缀可以伪造，
    且 M4 的解析路由要靠它决定走文本抽取还是 OCR。
    """

    content: bytes
    file_name: str
    content_type: str


@dataclass(frozen=True)
class WriteCommentResultDTO:
    """评论写入结果。

    `replayed=True` 表示外部系统识别出这是同一幂等键的重放、返回第一次的结果 ——
    这是幂等的正常表现，**不是错误**。
    """

    write_status: WriteStatus
    external_comment_id: str | None = None
    replayed: bool = False
    #: 外部系统返回的原始文本，仅用于排障；门禁拒绝时为空（根本没有发起调用）
    response_text: str | None = None


# ============================================================
# 端口
# ============================================================


@runtime_checkable
class ApprovalReadGateway(Protocol):
    """**读取**能力。

    ⚠️ `runtime_checkable` 的 `isinstance` 只检查**成员是否存在**，不校验签名；
    语义一致性由 Adapter 合约测试保证。
    """

    #: 本适配器对接的**审批平台标识**（`mock` / `dingtalk` / `feishu`）。
    #: 身份放在端口上而不是由服务读配置：导入去重键是
    #: `provider + tenant_id + instance_id`，两边各取一份会出现
    #: "同一个审批单被重复建任务，且两边的键永远对不上"。
    provider: str

    #: 租户标识（轻量租户，v1 固定 `default`）
    tenant_id: str

    def list_pending(self, limit: int) -> list[PendingApprovalDTO]:
        """拉取待处理审批单列表。"""
        ...

    def get_detail(self, instance_id: str) -> ApprovalDetailDTO:
        """查询审批单详情。

        实例不存在时抛 `PermanentGatewayError(INSTANCE_NOT_FOUND)`，
        而不是返回字段全空的 DTO —— 否则"查不到"与"字段缺失"无法区分。
        """
        ...

    def download_attachment(
        self, instance_id: str, attachment_id: str
    ) -> DownloadedAttachmentDTO:
        """下载附件字节流；附件已被删除时抛 `PermanentGatewayError(ATTACHMENT_MISSING)`。"""
        ...


@runtime_checkable
class ApprovalCommentGateway(Protocol):
    """**评论**能力。只有具备回写权限的实现才需要满足它。"""

    def write_comment(
        self,
        instance_id: str,
        content: str,
        *,
        idempotency_key: str,
        operator_name: str | None = None,
    ) -> WriteCommentResultDTO:
        """写入审查意见。

        ⚠️ `idempotency_key` 是**必填关键字参数**，刻意不给默认值：
        它必须由回写模块基于内容摘要生成，一旦有默认值，
        总有一天会有人忘了传而依赖默认值 —— 那时幂等就静默失效了。
        """
        ...

    def get_write_result(
        self, instance_id: str, idempotency_key: str
    ) -> WriteCommentResultDTO | None:
        """按幂等键查询写入结果，查不到返回 `None`。

        用途：外部调用超时后**先查再决定是否重发** —— 超时不代表对方没写成功，
        直接重发可能造成重复评论。
        """
        ...


@runtime_checkable
class ApprovalGateway(ApprovalReadGateway, ApprovalCommentGateway, Protocol):
    """同时具备读取与评论能力的实现（Mock、自建审批系统）。

    业务代码**不应**依赖这个组合接口，而应依赖自己真正需要的那一个窄接口；
    它的意义是给"两种能力都有"的实现一个类型标签，并让合约测试一次覆盖全部方法。
    """
