"""mock 审批系统的状态存储。

模拟的是**外部业务系统**的状态，因此刻意保持独立：
不 import `app.*`，只依赖标准库 + `sample_pdf`。

三个关键点：

1. **待办数据是业务事实**——`our_party_name` / `our_party_contract_label` /
   `our_party_business_role` / `contract_type` 由"申请人填写"，
   不是从合同文本推断出来的。这正是设计文档 §3.1 要求的三类信息分离。

2. **评论回写按 `idempotency_key` 幂等**（设计决议 D6）。
   幂等由**外部系统与审查系统双方共同保证**：
   审查系统用数据库唯一约束兜底，mock 这边用"同键返回首次结果"配合。

3. **线程安全**——uvicorn 会把同步端点丢进线程池，因此所有状态访问都加锁。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from mock_approval import StrEnum

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FIXTURES = Path(__file__).resolve().parent / "fixtures.json"
#: 若该目录下存在同名文件，则优先返回真实文件。
#: 该优先级保留给**真实合同**（M12 的黄金合同集 / 生产接入）。
CONTRACTS_DIR = PROJECT_ROOT / "data" / "contracts"

#: M4 起的**中文合成合同**夹具目录 —— 由 `scripts/make_fixtures.py` 生成并提交进仓库。
#:
#: 它与 `CONTRACTS_DIR` 是**两件事**：
#:   - 这里放的是**开发与验收**用的合成合同，内容随代码一起版本化；
#:   - `CONTRACTS_DIR` 放真实合同，属运行时数据，不进仓库。
#:
#: ⚠️ 本目录只在**开发期**由脚本写入。运行时的 `mock_approval` 只**读**它，
#:    因此本模块与 `main.py` 都不 import PyMuPDF（"外部对接方不依赖主项目第三方库"）。
FIXTURES_DIR = PROJECT_ROOT / "mock_approval" / "fixtures"


class WriteOutcome(StrEnum):
    """评论写入的结果。"""

    CREATED = "created"
    #: 同键 + 同请求 → 重放，返回第一次的结果
    REPLAYED = "replayed"
    #: 同键 + **不同请求** → 幂等键被复用，必须拒绝
    CONFLICT = "conflict"


@dataclass(frozen=True)
class Attachment:
    attachment_id: str
    file_name: str
    file_type: str
    content_kind: str  # text_pdf / scan_pdf / missing

    @property
    def available(self) -> bool:
        """附件是否可下载。

        `missing` 表示该附件在审批系统中已被删除——下载必然失败，
        用于演示"附件缺失 → blocked → 人工重试"。
        """
        return self.content_kind != "missing"

    def to_dict(self) -> dict[str, Any]:
        return {
            "attachment_id": self.attachment_id,
            "file_name": self.file_name,
            "file_type": self.file_type,
            "available": self.available,
        }


@dataclass(frozen=True)
class Instance:
    """一份审批单。"""

    approval_code: str
    approval_title: str
    applicant_name: str
    apply_time: str
    # ---- 权威审查上下文（业务事实）----
    our_party_name: str
    our_party_contract_label: str
    our_party_business_role: str
    contract_type: str
    form_data: dict[str, Any] = field(default_factory=dict)
    attachments: tuple[Attachment, ...] = ()

    def to_pending_dict(self) -> dict[str, Any]:
        """待办列表项（对应需求 2.4.3 的字段要求）。"""
        return {
            "approval_code": self.approval_code,
            "approval_title": self.approval_title,
            "applicant_name": self.applicant_name,
            "apply_time": self.apply_time,
            "attachment_count": len(self.attachments),
        }

    def to_detail_dict(self) -> dict[str, Any]:
        """审批单详情：审批信息 + 表单数据 + 附件信息 + 权威审查上下文。"""
        return {
            **self.to_pending_dict(),
            "our_party_name": self.our_party_name,
            "our_party_contract_label": self.our_party_contract_label,
            "our_party_business_role": self.our_party_business_role,
            "contract_type": self.contract_type,
            "form_data": self.form_data,
            "attachments": [a.to_dict() for a in self.attachments],
        }


class MockStore:
    """待办数据与评论的内存存储。"""

    def __init__(self, fixtures_path: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._instances: dict[str, Instance] = {}
        self._comments: dict[str, list[dict[str, Any]]] = {}
        #: idempotency_key → (请求指纹 (审批单号, 内容), 首次回写的结果)
        #: 保存指纹是为了区分"同键同请求的重放"与"同键不同请求的键冲突"
        self._idempotency: dict[str, tuple[tuple[str, str], dict[str, Any]]] = {}
        self._comment_seq = 0
        self._load(fixtures_path or DEFAULT_FIXTURES)

    # ------------------------------------------------------------------
    # 装载
    # ------------------------------------------------------------------
    def _load(self, path: Path) -> None:
        raw = json.loads(path.read_text(encoding="utf-8"))
        for item in raw.get("instances", []):
            attachments = tuple(
                Attachment(
                    attachment_id=att["attachment_id"],
                    file_name=att["file_name"],
                    file_type=att.get("file_type", "pdf"),
                    content_kind=att.get("content_kind", "text_pdf"),
                )
                for att in item.get("attachments", [])
            )
            instance = Instance(
                approval_code=item["approval_code"],
                approval_title=item["approval_title"],
                applicant_name=item["applicant_name"],
                apply_time=item["apply_time"],
                our_party_name=item["our_party_name"],
                our_party_contract_label=item["our_party_contract_label"],
                our_party_business_role=item["our_party_business_role"],
                contract_type=item["contract_type"],
                form_data=item.get("form_data", {}),
                attachments=attachments,
            )
            self._instances[instance.approval_code] = instance

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def list_pending(self, limit: int) -> list[Instance]:
        with self._lock:
            return list(self._instances.values())[:limit]

    def get(self, instance_id: str) -> Instance | None:
        with self._lock:
            return self._instances.get(instance_id)

    def find_attachment(
        self, instance_id: str, attachment_id: str
    ) -> Attachment | None:
        instance = self.get(instance_id)
        if instance is None:
            return None
        for attachment in instance.attachments:
            if attachment.attachment_id == attachment_id:
                return attachment
        return None

    # ------------------------------------------------------------------
    # 评论回写
    # ------------------------------------------------------------------
    def write_comment(
        self,
        instance_id: str,
        content: str,
        idempotency_key: str,
        operator_name: str | None,
    ) -> tuple[dict[str, Any] | None, WriteOutcome]:
        """写入评论，按幂等键去重。

        幂等语义（决议 D6 ＋ 幂等键的通用契约）：

        ==========================  ==============  ==================================
        情形                        结果           行为
        ==========================  ==============  ==================================
        键首次出现                  CREATED         写入并返回新记录
        同键 + 同审批单 + 同内容     REPLAYED        返回**第一次**的结果，不重复写入
        同键 + 内容或审批单不同      CONFLICT        **不写入，也不返回别人的结果**
        ==========================  ==============  ==================================

        第三种情形必须单独处理：幂等键代表"这一个请求"，
        若被复用到内容不同的请求上，直接把前一个请求的结果返回给调用方，
        会让调用方误以为自己的意见已经写进去了——这是比重复写入更危险的错误。
        正确做法是拒绝，并要求调用方换一个新的幂等键。
        """
        # 请求指纹：用于判断"同一个键是否被用在了同一个请求上"
        fingerprint = (instance_id, content)

        with self._lock:
            existing = self._idempotency.get(idempotency_key)
            if existing is not None:
                stored_fingerprint, record = existing
                if stored_fingerprint != fingerprint:
                    return None, WriteOutcome.CONFLICT
                return {**record, "replayed": True}, WriteOutcome.REPLAYED

            self._comment_seq += 1
            record = {
                "comment_id": f"C-{self._comment_seq:06d}",
                "instance_id": instance_id,
                "content": content,
                "operator_name": operator_name,
                "idempotency_key": idempotency_key,
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }
            self._comments.setdefault(instance_id, []).append(record)
            self._idempotency[idempotency_key] = (fingerprint, record)
            return {**record, "replayed": False}, WriteOutcome.CREATED

    def list_comments(self, instance_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._comments.get(instance_id, ()))

    def comment_count(self, instance_id: str) -> int:
        return len(self.list_comments(instance_id))

    def reset_runtime_state(self) -> None:
        """清空评论与幂等记录（测试与演示重置用）；**待办数据不受影响**。

        注意：演示"幂等"时不要调用它——否则第二次调用就不再是重放，
        而会真的写入第二条评论。
        """
        with self._lock:
            self._comments.clear()
            self._idempotency.clear()
            self._comment_seq = 0


#: 单例。生产用法是 `python -m uvicorn mock_approval.main:app`，
#: 模块级单例保证所有请求共享同一份状态。
store = MockStore()
