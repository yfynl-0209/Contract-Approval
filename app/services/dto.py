"""服务返回结构（工具输出）。

跨边界的数据形状分三类，各有归属**不混用**：

    app/ports/*.py       外部系统的内部标准化 DTO    由适配器产生
    app/schemas.py       **输入**校验模型            由人工 / API 调用方提供
    app/services/dto.py  **服务返回结构**（本模块）   由应用服务产生

分开的理由很具体：`schemas.py` 用 `extra="forbid"` 严格校验**输入**，而返回结构是
系统自己生成的；用同一套严格模型会把"内部改字段"变成"外部调用失败"。

用 frozen dataclass 而非 Pydantic：它们只在进程内传递，不参与 JSON 解析与校验。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ============================================================
# 通用
# ============================================================


@dataclass(frozen=True)
class TaskRef:
    """长任务同步等待超时后的返回。

    它**不改工具签名**，只改变返回形态（企业化设计 §6）：调用方拿到 id 后去
    `status_url` 查询进度，而不必让 HTTP 请求一直挂着。
    """

    task_id: int
    status: str
    status_url: str


# ============================================================
# 工具 1：待办拉取
# ============================================================


@dataclass(frozen=True)
class TaskOutcome:
    """单个审批单在本次拉取中的处理结果。"""

    task_id: int
    instance_id: str
    approval_code: str
    #: True = 新建任务；False = 命中既有任务并刷新字段
    created: bool


@dataclass(frozen=True)
class PullResult:
    """工具 1 `list_pending_contract_approvals` 的返回。"""

    provider: str
    tenant_id: str
    fetched: int  #: 外部系统返回的条数
    created: int  #: 其中新建的任务数
    updated: int  #: 其中命中既有任务、只做刷新的数量
    items: tuple[TaskOutcome, ...]
    job_idempotency_key: str  #: 作业幂等键，可据此在作业表里定位本次执行

    @property
    def task_ids(self) -> tuple[int, ...]:
        """本次涉及的既有与新建任务 id。"""
        return tuple(item.task_id for item in self.items)


# ============================================================
# 工具 2：审批详情
# ============================================================


@dataclass(frozen=True)
class AttachmentSummary:
    """附件元数据摘要（**不含字节内容**）。"""

    attachment_id: str
    file_name: str
    file_type: str
    available: bool  #: 外部系统标记该附件是否仍可下载
    download_status: str  #: pending / success / failed
    is_new: bool  #: 本次同步是新建记录还是更新既有记录


@dataclass(frozen=True)
class DownloadResult:
    """工具 3 `download_contract_attachment` 的返回。

    ⚠️ `object_key` 是**内部**长期保存位置，**不下发到接口响应**；
    `file_path` 是受控临时物化路径（相对 `storage_root`），供后续解析工具读取，
    调用端不得把它当成可绕过鉴权的永久公开地址。
    """

    task_id: int
    attachment_id: str
    file_name: str
    file_size: int
    file_checksum: str
    object_key: str
    file_path: str
    content_type: str
    download_status: str


@dataclass(frozen=True)
class ApprovalDetailResult:
    """工具 2 `get_contract_approval` 的返回。"""

    task_id: int
    instance_id: str
    approval_code: str
    task_status: str
    context_status: str  #: complete / missing / conflict / confirmed
    our_party_name: str | None
    our_party_contract_label: str | None
    our_party_business_role: str | None
    contract_type: str | None
    form_data: dict[str, Any] = field(default_factory=dict)  #: 审批表单原样（已落库）
    attachments: tuple[AttachmentSummary, ...] = ()
