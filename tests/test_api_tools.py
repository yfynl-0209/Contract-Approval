"""工具的 REST 入口测试（M3 / T8）。

三个工具的业务行为已由 `test_pull_service.py` / `test_attachment_service.py` 覆盖，
本文件只测**接口层特有的那几件事**：

1. **异常 → 状态码**的映射是否正确，尤其是"业务结论返回 200"；
2. **事务边界**：失败响应之后，作业与任务状态必须**已经落库**；
3. 请求校验（未知字段、空白标识）在进入业务逻辑之前就被拦住。

第 2 条最容易被忽略却后果最大：如果失败路径回滚，
调用端只看到一句"503"，无从知道系统试过几次、下次何时重试 ——
而排查失败恰恰最需要这些记录。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.adapters.storage.local_file_storage import LocalFileStorage
from app.auth import Actor, Role
from app.api.deps import get_actor, get_db, get_gateway, get_storage
from app.config import settings
from app.db import Base, transactional_session
from app.enums import DownloadStatus, ErrorCode, JobStatus, TaskStatus
from app.errors import (
    AppError,
    AttachmentValidationError,
    PermanentGatewayError,
    TransientGatewayError,
)
from app.main import app
from app.models import ApprovalAttachment, ApprovalTask, WorkflowJob
from app.ports.approval_gateway import (
    ApprovalDetailDTO,
    AttachmentDTO,
    AuthoritativeContextDTO,
    DownloadedAttachmentDTO,
    PendingApprovalDTO,
)

#: 全权限测试主体（`system_admin` 是唯一覆盖全部 8 项权限的角色）。
#:
#: 本文件测的是**业务行为**，不是身份与授权 —— 装一个全权限主体，
#: 是为了让身份层在测试里"透明"：某个端点日后新增一条权限要求时，
#: 这里不会因为与测试目标无关的原因变红（那种红只会让人去改断言，
#: 而不是去看真正的问题）。
#:
#: ⚠️ **代价必须说清楚**：这些用例**不覆盖**"无权限的人被拒绝"。
#: 401 / 403 / 令牌校验 / 角色映射在 `tests/test_auth_rbac.py` 里验。
_FULL_ACCESS_ACTOR = Actor(
    actor_id="test-actor",
    display_name="test-actor",
    roles=frozenset({Role.SYSTEM_ADMIN.value}),
    tenant_id="default",
)

PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF"


# ============================================================
# 假适配器与测试台
# ============================================================


class _FakeGateway:
    """实现三个读取方法 + 身份成员，用于替换真实网关。"""

    provider = "mock"
    tenant_id = "default"

    def __init__(self) -> None:
        self.pendings: list[PendingApprovalDTO] = [_pending("HT-1"), _pending("HT-2")]
        self.detail: ApprovalDetailDTO = _detail("HT-1")
        self.content: bytes = PDF_BYTES
        self.file_name: str = "contract.pdf"
        self.error: Exception | None = None
        self.download_error: Exception | None = None

    def list_pending(self, limit: int) -> list[PendingApprovalDTO]:
        if self.error is not None:
            raise self.error
        return list(self.pendings)[:limit]

    def get_detail(self, instance_id: str) -> ApprovalDetailDTO:
        if self.error is not None:
            raise self.error
        return self.detail

    def download_attachment(
        self, instance_id: str, attachment_id: str
    ) -> DownloadedAttachmentDTO:
        if self.download_error is not None:
            raise self.download_error
        if self.error is not None:
            raise self.error
        return DownloadedAttachmentDTO(
            content=self.content,
            file_name=self.file_name,
            content_type="application/pdf",
        )


class NotFoundError(PermanentGatewayError):
    """测试辅助：一个稳定的"确定性网关错误"。"""

    def __init__(self) -> None:
        super().__init__("模拟确定性失败", code=ErrorCode.INVALID_GATEWAY_RESPONSE)


def _pending(code: str) -> PendingApprovalDTO:
    return PendingApprovalDTO(
        provider="mock",
        tenant_id="default",
        instance_id=code,
        approval_code=code,
        approval_title="测试合同",
        applicant_name="张三",
        apply_time="2026-09-01 09:00:00",
        attachment_count=1,
    )


def _detail(code: str) -> ApprovalDetailDTO:
    return ApprovalDetailDTO(
        instance_id=code,
        approval_code=code,
        approval_title="测试合同",
        applicant_name="张三",
        apply_time="2026-09-01 09:00:00",
        context=AuthoritativeContextDTO(
            our_party_name="示例科技有限公司",
            our_party_contract_label="party_a",
            our_party_business_role="buyer",
            contract_type="procurement",
        ),
        form_data={"amount": "1200000"},
        attachments=(
            AttachmentDTO(attachment_id="A-1", file_name="contract.pdf", file_type="pdf"),
        ),
    )


class Harness:
    """一次测试用的完整装配：临时库 + 假网关 + 临时存储。"""

    def __init__(self, work_dir: Path) -> None:
        self.engine = create_engine(
            f"sqlite:///{(work_dir / 'api.db').as_posix()}",
            future=True,
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(
            bind=self.engine, autoflush=False, autocommit=False, future=True
        )
        self.gateway = _FakeGateway()
        self.storage = LocalFileStorage(work_dir / "storage" / "objects")
        self.client = TestClient(app)

    def install(self) -> None:
        """把依赖替换成测试实现。

        这正是"依赖倒置"落到框架上的形态：生产装配真实适配器，
        测试装配假实现，**业务代码一行都不用改**。
        """

        def session_dependency():
            # 复用 `app/db.py` 的**同一份**事务边界实现。
            # 这里若另写一份，两边语义一旦分叉，
            # 测出来的事务行为与生产不一致 —— 等于没测。
            yield from transactional_session(self.session_factory())

        app.dependency_overrides[get_db] = session_dependency
        app.dependency_overrides[get_gateway] = lambda: self.gateway
        # 身份层在本文件里"透明"（理由见 _FULL_ACCESS_ACTOR 的注释）
        app.dependency_overrides[get_actor] = lambda: _FULL_ACCESS_ACTOR
        app.dependency_overrides[get_storage] = lambda: self.storage

    def uninstall(self) -> None:
        app.dependency_overrides.clear()
        self.engine.dispose()

    def seed_task(self, instance_id: str = "HT-1") -> int:
        with self.session_factory() as session:
            task = ApprovalTask(
                provider="mock",
                tenant_id="default",
                instance_id=instance_id,
                approval_code=instance_id,
                context_status="complete",
            )
            session.add(task)
            session.commit()
            return task.id

    def tasks(self) -> list[ApprovalTask]:
        with self.session_factory() as session:
            return list(session.execute(select(ApprovalTask)).scalars().all())

    def jobs(self) -> list[WorkflowJob]:
        with self.session_factory() as session:
            return list(session.execute(select(WorkflowJob)).scalars().all())

    def attachments(self) -> list[ApprovalAttachment]:
        with self.session_factory() as session:
            return list(session.execute(select(ApprovalAttachment)).scalars().all())


@pytest.fixture()
def harness(work_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    monkeypatch.setattr(settings, "storage_root", str(work_dir / "storage"))
    monkeypatch.setattr(settings, "attachment_allowed_types", "application/pdf")
    monkeypatch.setattr(settings, "attachment_max_bytes", 1024 * 1024)

    built = Harness(work_dir)
    built.install()
    try:
        yield built
    finally:
        built.uninstall()


# ============================================================
# 1. 三个工具的正常路径
# ============================================================


def test_tool1_pull_returns_pulled_outcome(harness: Harness) -> None:
    response = harness.client.post(
        "/tools/list_pending_contract_approvals", json={"limit": 10}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "pulled"
    assert body["fetched"] == 2
    assert body["created"] == 2
    assert body["updated"] == 0
    assert len(body["items"]) == 2
    assert body["provider"] == "mock"
    assert body["job_idempotency_key"].startswith("pull:mock:default:")


def test_tool1_pull_is_idempotent_across_calls(harness: Harness) -> None:
    """重复拉取必须是无害的 —— 这是 M3 的完成标志之一。"""
    harness.client.post("/tools/list_pending_contract_approvals", json={"limit": 10})
    second = harness.client.post(
        "/tools/list_pending_contract_approvals", json={"limit": 10}
    )

    assert second.json()["created"] == 0
    assert second.json()["updated"] == 2
    assert len(harness.tasks()) == 2


def test_tool2_detail_returns_synced_outcome(harness: Harness) -> None:
    response = harness.client.post(
        "/tools/get_contract_approval", json={"instance_id": "HT-1"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "synced"
    assert body["context_status"] == "complete"
    assert body["our_party_business_role"] == "buyer"
    assert body["form_data"] == {"amount": "1200000"}
    assert body["attachments"][0]["attachment_id"] == "A-1"


def test_tool3_download_returns_artifact_locations(harness: Harness) -> None:
    """工具 3 返回**受控临时物化路径**，但**不返回内部对象键**。

    需求 2.4.4 明确要求该工具返回本地文件路径（此处是相对路径 `workspace/…`），
    因此 `file_path` 必须保留。而对象键属于内部实现细节：

    - 它是 `sha256/<前2位>/<次2位>/<摘要>.<ext>` 这一**具体存储布局**；
    - 下发会让外部调用方**依赖它**，而 M9 把本地文件换成 MinIO 后，
      这个键的命名空间**不再是我们的自由** —— 那时就从"实现替换"变成了"破坏性变更"。

    这里**反向断言它不存在**。正向断言（"有这个字段"）挡不住回归：
    某次"顺手补个字段"就会把它放回去，而那种改动**不会让任何测试变红**。

    同时断言内部仍然真的写进了对象存储 —— 从数据库读，而不是要求响应返回。
    这两条一起才说明"对外收紧了、对内没丢"。
    """
    harness.seed_task()

    response = harness.client.post(
        "/tools/download_contract_attachment",
        json={"instance_id": "HT-1", "attachment_id": "A-1"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "downloaded"

    # 对外：不得下发内部对象键
    assert "object_key" not in body, "对象键是内部实现细节，不得出现在响应里"

    # 对外：file_path 必须是受控相对路径，不能是绝对路径
    assert body["file_path"].startswith("workspace/")
    assert not body["file_path"].startswith("/")

    # 对内：对象确实落在内容寻址位置上（从库里读，与响应无关）
    record = harness.attachments()[0]
    assert record.object_key is not None
    assert record.object_key.startswith("sha256/")
    assert body["download_status"] == DownloadStatus.SUCCESS.value
    assert body["file_size"] == len(PDF_BYTES)


def test_tool3_download_records_attachment(harness: Harness) -> None:
    harness.seed_task()

    harness.client.post(
        "/tools/download_contract_attachment",
        json={"instance_id": "HT-1", "attachment_id": "A-1"},
    )

    records = harness.attachments()
    assert len(records) == 1
    assert records[0].download_status == DownloadStatus.SUCCESS.value
    assert records[0].file_checksum is not None


# ============================================================
# 2. "业务结论"必须返回 200，而不是 4xx/5xx
# ============================================================


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (
            PermanentGatewayError(
                "附件 A-1 在审批系统中已被删除", code=ErrorCode.ATTACHMENT_MISSING
            ),
            "ATTACHMENT_MISSING",
        ),
        (
            AttachmentValidationError("附件内容为空", code=ErrorCode.ATTACHMENT_EMPTY),
            "ATTACHMENT_EMPTY",
        ),
        (
            AttachmentValidationError(
                "大小超过上限", code=ErrorCode.ATTACHMENT_TOO_LARGE
            ),
            "ATTACHMENT_TOO_LARGE",
        ),
        (
            AttachmentValidationError(
                "类型不在白名单内", code=ErrorCode.ATTACHMENT_TYPE_NOT_ALLOWED
            ),
            "ATTACHMENT_TYPE_NOT_ALLOWED",
        ),
    ],
    ids=["附件已被删除", "空文件", "超过上限", "类型不符"],
)
def test_business_fact_returns_200_with_blocked_outcome(
    harness: Harness, error: AppError, expected_code: str
) -> None:
    """**这四条是"业务结论"，不是系统故障，必须返回 200。**

    用 5xx 会让调用端把它当成"服务抖动稍后重试"，于是任务永远等不到人工处理；
    用 4xx 又暗示"请求写错了"，而请求完全正确。

    正确的语义是：**调用成功确认了一个业务事实** ——
    这份合同的附件有问题，任务已 `blocked`，需要人来看。
    """
    harness.seed_task()
    harness.gateway.download_error = error

    response = harness.client.post(
        "/tools/download_contract_attachment",
        json={"instance_id": "HT-1", "attachment_id": "A-1"},
    )

    assert response.status_code == 200, "业务结论不该是 4xx/5xx"
    body = response.json()
    assert body["outcome"] == "blocked"
    assert body["error_code"] == expected_code
    assert body["retryable"] is False
    assert body["instance_id"] == "HT-1"


def test_blocked_outcome_reports_real_task_state(harness: Harness) -> None:
    """返回体里的任务状态必须是**查出来的真实状态**。

    如果只是从异常推断（"既然是附件缺失，那任务肯定 blocked 了"），
    那么服务真没阻塞任务时，响应依然"看起来很对"——
    缺陷就被永久藏住了。
    """
    harness.seed_task()
    harness.gateway.download_error = PermanentGatewayError(
        "附件 A-1 在审批系统中已被删除", code=ErrorCode.ATTACHMENT_MISSING
    )

    body = harness.client.post(
        "/tools/download_contract_attachment",
        json={"instance_id": "HT-1", "attachment_id": "A-1"},
    ).json()

    assert body["task_status"] == TaskStatus.BLOCKED.value
    assert body["blocked_stage"] == "download"
    assert body["task_id"] == harness.tasks()[0].id
    # 并且确实落库了 —— 响应里的状态不是编出来的
    assert harness.tasks()[0].task_status == TaskStatus.BLOCKED.value


def test_blocked_response_is_not_an_error_shape(harness: Harness) -> None:
    """阻塞响应与错误响应必须能区分：前者有 `task_status`，后者有 `retryable`。"""
    harness.seed_task()
    harness.gateway.download_error = PermanentGatewayError(
        "附件已被删除", code=ErrorCode.ATTACHMENT_MISSING
    )

    blocked = harness.client.post(
        "/tools/download_contract_attachment",
        json={"instance_id": "HT-1", "attachment_id": "A-1"},
    ).json()

    assert blocked["outcome"] == "blocked"
    assert blocked["task_status"] is not None


# ============================================================
# 3. 系统故障 → 5xx，并且**状态必须落库**
# ============================================================


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (
            TransientGatewayError("超时", code=ErrorCode.APPROVAL_API_TIMEOUT),
            503,
        ),
        (TransientGatewayError("5xx", code=ErrorCode.APPROVAL_API_ERROR), 503),
        (
            PermanentGatewayError("鉴权失败", code=ErrorCode.AUTH_FAILED),
            502,
        ),
        (
            PermanentGatewayError(
                "响应结构不符", code=ErrorCode.INVALID_GATEWAY_RESPONSE
            ),
            502,
        ),
    ],
    ids=["超时", "服务端错误", "鉴权失败", "响应结构不符"],
)
def test_system_failure_returns_mapped_status(
    harness: Harness, error: AppError, expected_status: int
) -> None:
    harness.seed_task()
    harness.gateway.download_error = error

    response = harness.client.post(
        "/tools/download_contract_attachment",
        json={"instance_id": "HT-1", "attachment_id": "A-1"},
    )

    assert response.status_code == expected_status
    assert response.json()["error_code"] == str(error.code)
    assert response.json()["outcome"] == "error"


def test_transient_failure_sets_retry_after_header(harness: Harness) -> None:
    """瞬时故障要显式告诉调用端"现在重试没用"。"""
    harness.seed_task()
    harness.gateway.download_error = TransientGatewayError(
        "超时", code=ErrorCode.APPROVAL_API_TIMEOUT
    )

    response = harness.client.post(
        "/tools/download_contract_attachment",
        json={"instance_id": "HT-1", "attachment_id": "A-1"},
    )

    assert response.status_code == 503
    assert response.headers["retry-after"] == "30"


def test_permanent_failure_has_no_retry_after_header(harness: Harness) -> None:
    """确定性故障不该诱导重试 —— 加 `Retry-After` 等于在鼓励无意义重试。"""
    harness.seed_task()
    harness.gateway.download_error = PermanentGatewayError(
        "鉴权失败", code=ErrorCode.AUTH_FAILED
    )

    response = harness.client.post(
        "/tools/download_contract_attachment",
        json={"instance_id": "HT-1", "attachment_id": "A-1"},
    )

    assert "retry-after" not in response.headers


def test_failed_request_still_persists_job_state(harness: Harness) -> None:
    """**失败响应之后，作业的重试状态必须已经落库。**

    这是"事务边界放在依赖里、失败路径也提交"的核心验证。
    若失败路径回滚，调用端只会看到一句 503，
    无从知道系统试过几次、下次何时重试 —— 而这些恰恰是排查最需要的记录。
    """
    harness.seed_task()
    harness.gateway.download_error = TransientGatewayError(
        "超时", code=ErrorCode.APPROVAL_API_TIMEOUT
    )

    response = harness.client.post(
        "/tools/download_contract_attachment",
        json={"instance_id": "HT-1", "attachment_id": "A-1"},
    )

    assert response.status_code == 503

    jobs = harness.jobs()
    assert len(jobs) == 1, "作业记录随失败响应一起丢了"
    assert jobs[0].job_status == JobStatus.RETRY_WAIT.value
    assert jobs[0].last_error_code == ErrorCode.APPROVAL_API_TIMEOUT.value
    assert jobs[0].next_retry_at is not None

    # 重试未耗尽 → 任务**不该**被阻塞（一次抖动不该产生人工工单）
    assert harness.tasks()[0].task_status != TaskStatus.BLOCKED.value


def test_tool1_transient_failure_returns_503(harness: Harness) -> None:
    harness.gateway.error = TransientGatewayError(
        "连不上", code=ErrorCode.APPROVAL_UNREACHABLE
    )

    response = harness.client.post(
        "/tools/list_pending_contract_approvals", json={"limit": 10}
    )

    assert response.status_code == 503
    assert response.headers["retry-after"] == "30"
    assert response.json()["error_code"] == "APPROVAL_UNREACHABLE"


def test_tool1_failure_does_not_leave_partial_tasks(harness: Harness) -> None:
    """拉取失败不得留下"拉了一半"的任务，但作业台账与日志要保留。"""
    harness.gateway.error = NotFoundError()

    response = harness.client.post(
        "/tools/list_pending_contract_approvals", json={"limit": 10}
    )

    assert response.status_code == 502
    assert harness.tasks() == []
    assert len(harness.jobs()) == 1


def test_instance_not_found_maps_to_404(harness: Harness) -> None:
    """外部系统说"没这个单子" → 404（目标不存在），不是 5xx。"""
    harness.gateway.error = PermanentGatewayError(
        "审批单不存在", code=ErrorCode.INSTANCE_NOT_FOUND
    )

    response = harness.client.post(
        "/tools/get_contract_approval", json={"instance_id": "HT-NOPE"}
    )

    assert response.status_code == 404
    assert response.json()["error_code"] == "INSTANCE_NOT_FOUND"


def test_task_not_found_maps_to_404(harness: Harness) -> None:
    """本系统没有这条任务 → 404，且消息要告诉调用方下一步做什么。"""
    response = harness.client.post(
        "/tools/download_contract_attachment",
        json={"instance_id": "HT-NOT-PULLED", "attachment_id": "A-1"},
    )

    assert response.status_code == 404
    body = response.json()
    assert body["error_code"] == "TASK_NOT_FOUND"
    assert "拉取" in body["message"]


# ============================================================
# 4. 请求校验：在进入业务逻辑之前就拦住
# ============================================================


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/tools/list_pending_contract_approvals", {"limit": 0}),
        ("/tools/list_pending_contract_approvals", {"limit": 9999}),
        ("/tools/list_pending_contract_approvals", {"limit": 10, "unknown": 1}),
        ("/tools/get_contract_approval", {"instance_id": ""}),
        ("/tools/get_contract_approval", {"instance_id": "   "}),
        ("/tools/get_contract_approval", {}),
        (
            "/tools/download_contract_attachment",
            {"instance_id": "HT-1", "attachment_id": "  "},
        ),
    ],
    ids=[
        "limit为零",
        "limit超上限",
        "未知字段",
        "实例号空串",
        "实例号纯空白",
        "缺少必填字段",
        "附件号纯空白",
    ],
)
def test_invalid_requests_are_rejected_with_422(
    harness: Harness, path: str, payload: dict
) -> None:
    """请求校验必须在**进入业务逻辑之前**完成。

    "纯空白"这条尤其重要：`min_length` 单独拦不住 `"   "`，
    必须配合 `str_strip_whitespace=True`。放它进去会拼出
    `/api/instances//comments` 这类畸形路径，外部系统返回 404，
    而我们报"审批单不存在"—— 归因完全错位。
    """
    response = harness.client.post(path, json=payload)

    assert response.status_code == 422


def test_blank_identifiers_never_reach_the_gateway(harness: Harness) -> None:
    """校验失败时不该发出任何外部调用。"""
    calls: list[str] = []
    original = harness.gateway.list_pending

    def spy(limit: int):
        calls.append("called")
        return original(limit)

    harness.gateway.list_pending = spy  # type: ignore[method-assign]

    harness.client.post("/tools/list_pending_contract_approvals", json={"limit": 0})

    assert calls == []


# ============================================================
# 5. 接口层不写业务判断
# ============================================================


def test_every_tool_response_carries_outcome(harness: Harness) -> None:
    """三个工具的响应都带 `outcome`，自动化流程据此分支而不必猜。

    这条同时是一道**结构约束**：如果哪天有人往接口层加了业务分支并
    改了返回结构，`outcome` 缺失会让这条测试立刻失败。
    """
    harness.seed_task()

    responses = [
        harness.client.post(
            "/tools/list_pending_contract_approvals", json={"limit": 10}
        ),
        harness.client.post("/tools/get_contract_approval", json={"instance_id": "HT-1"}),
        harness.client.post(
            "/tools/download_contract_attachment",
            json={"instance_id": "HT-1", "attachment_id": "A-1"},
        ),
    ]

    for response in responses:
        assert response.status_code == 200
        assert response.json()["outcome"] in {
            "pulled",
            "synced",
            "downloaded",
            "blocked",
        }


def _run_all_tools(harness: Harness) -> None:
    harness.client.post("/tools/list_pending_contract_approvals", json={"limit": 10})
    harness.client.post("/tools/get_contract_approval", json={"instance_id": "HT-1"})
    harness.client.post(
        "/tools/download_contract_attachment",
        json={"instance_id": "HT-1", "attachment_id": "A-1"},
    )


def test_each_tool_writes_a_job(harness: Harness) -> None:
    """**验收 13**：三个工具**各写一条** `workflow_jobs`。

    作业台账是"这次调用发生过"的唯一机器可查记录。
    少一个，那一类失败在库里就查不到原因 ——
    详情同步曾经就是这样，失败时不留任何痕迹。
    """
    harness.seed_task()

    _run_all_tools(harness)

    kinds = sorted(job.job_type for job in harness.jobs())
    assert kinds == ["detail", "download", "pull"], f"三个工具应各写一条作业，实际 {kinds}"


def test_repeated_calls_stop_creating_jobs(harness: Harness) -> None:
    """同一输入版本重复调用**不再新建作业**（验收 13 后半句）。

    判据是**稳定**而不是"永远只有三条"：
    详情与下载的作业版本都取自**已存**值，所以"第一次真正写入数据"会把
    版本键推进一版；从第二轮之后才固定下来。

    这与"不得只含 `instance_id`"是同一件事的两面：
    版本必须能变（否则第二次同步被永久拒绝），但也必须**收敛**（否则重复调用会堆作业）。
    """
    harness.seed_task()

    _run_all_tools(harness)
    _run_all_tools(harness)  # 这一轮把详情/下载的版本推进一版
    settled = len(harness.jobs())

    _run_all_tools(harness)  # 版本已稳定

    assert len(harness.jobs()) == settled, "输入版本稳定后不得再新建作业"


def test_openapi_documents_all_three_tools(harness: Harness) -> None:
    """三个工具必须出现在 OpenAPI 文档里（M7 的 MCP 形态据此对齐）。"""
    schema = harness.client.get("/openapi.json").json()
    paths = schema["paths"]

    assert "/tools/list_pending_contract_approvals" in paths
    assert "/tools/get_contract_approval" in paths
    assert "/tools/download_contract_attachment" in paths
