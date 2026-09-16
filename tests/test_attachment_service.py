"""附件服务测试（M3 / T7）。

三块重点：

1. **落地正确性** —— SHA-256 与内容一致、对象键内容寻址、
   物化路径可读且落在受控工作目录内。
2. **失败分类** —— 确定性错误立刻 `blocked`；瞬时错误**先重试**，
   只有耗尽次数才 `blocked`。把后者写成前者，会让一次网络抖动
   产生一个需要人工介入的失败任务。
3. **不可信输入** —— 文件名来自外部系统的响应头，必须当作攻击者可影响的输入。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import Base
from app.enums import DownloadStatus, ErrorCode, JobStatus, TaskStatus
from app.errors import (
    AttachmentValidationError,
    PermanentGatewayError,
    TaskNotFound,
    TransientGatewayError,
    TransientStorageError,
)
from app.models import ApprovalAttachment, ApprovalTask, TaskLog, WorkflowJob
from app.adapters.storage.local_file_storage import LocalFileStorage
from app.ports.approval_gateway import DownloadedAttachmentDTO
from app.services.attachment_service import AttachmentService

PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF"


# ============================================================
# 夹具
# ============================================================


@pytest.fixture()
def session(work_dir: Path):
    engine = create_engine(
        f"sqlite:///{(work_dir / 'attach.db').as_posix()}", future=True
    )
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db_session:
            yield db_session
    finally:
        engine.dispose()


@pytest.fixture()
def storage_root(work_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把存储根指到临时目录，并设定一份可预测的校验配置。"""
    root = work_dir / "storage"
    monkeypatch.setattr(settings, "storage_root", str(root))
    monkeypatch.setattr(settings, "attachment_max_bytes", 1024 * 1024)
    # 注意改的是**原始字段**而不是 `allowed_attachment_types`：
    # 后者是派生属性（逗号分隔 → 元组），没有 setter
    monkeypatch.setattr(settings, "attachment_allowed_types", "application/pdf")
    return root


class _FakeReadGateway:
    """提供下载能力的假适配器。"""

    provider = "mock"
    tenant_id = "default"

    def __init__(
        self,
        *,
        content: bytes = PDF_BYTES,
        file_name: str = "contract.pdf",
        content_type: str = "application/pdf",
        error: Exception | None = None,
    ) -> None:
        self.content = content
        self.file_name = file_name
        self.content_type = content_type
        self.error = error
        self.calls = 0

    def download_attachment(
        self, instance_id: str, attachment_id: str
    ) -> DownloadedAttachmentDTO:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return DownloadedAttachmentDTO(
            content=self.content,
            file_name=self.file_name,
            content_type=self.content_type,
        )

    def list_pending(self, limit: int):  # pragma: no cover - 本测试不用
        raise NotImplementedError

    def get_detail(self, instance_id: str):  # pragma: no cover - 本测试不用
        raise NotImplementedError


def _seed_task(
    session: Session, instance_id: str = "HT-2026-0001", **overrides: object
) -> ApprovalTask:
    task = ApprovalTask(
        provider="mock",
        tenant_id="default",
        instance_id=instance_id,
        approval_code=instance_id,
        approval_title="测试合同",
        context_status="complete",
    )
    for key, value in overrides.items():
        setattr(task, key, value)
    session.add(task)
    session.commit()
    return task


def _build(
    session: Session, storage_root: Path, gateway: _FakeReadGateway
) -> AttachmentService:
    return AttachmentService(gateway, LocalFileStorage(storage_root / "objects"), session)


def _task(session: Session) -> ApprovalTask:
    session.expire_all()
    return session.execute(select(ApprovalTask)).scalars().one()


def _attachment(session: Session) -> ApprovalAttachment:
    session.expire_all()
    return session.execute(select(ApprovalAttachment)).scalars().one()


def _jobs(session: Session) -> list[WorkflowJob]:
    session.expire_all()
    return list(session.execute(select(WorkflowJob)).scalars().all())


def _logs(session: Session) -> list[TaskLog]:
    session.expire_all()
    return list(session.execute(select(TaskLog)).scalars().all())


# ============================================================
# 1. 成功路径
# ============================================================


def test_download_stores_blob_and_updates_record(
    session: Session, storage_root: Path
) -> None:
    """下载成功后：对象存储有内容、记录有摘要、任务状态未变。"""
    _seed_task(session)
    service = _build(session, storage_root, _FakeReadGateway())

    result = service.download_contract_attachment("HT-2026-0001", "A-1")
    session.commit()

    expected_digest = hashlib.sha256(PDF_BYTES).hexdigest()

    assert result.file_checksum == expected_digest
    assert result.file_size == len(PDF_BYTES)
    assert result.object_key.startswith("sha256/")
    assert result.object_key.endswith(".pdf")
    assert result.download_status == DownloadStatus.SUCCESS.value

    record = _attachment(session)
    assert record.object_key == result.object_key
    assert record.file_checksum == expected_digest
    assert record.file_size == len(PDF_BYTES)
    assert record.content_type == "application/pdf"
    assert record.download_status == DownloadStatus.SUCCESS.value
    assert record.error_message is None


def test_object_storage_content_matches_downloaded_bytes(
    session: Session, storage_root: Path
) -> None:
    """取回的字节必须与下载内容完全一致（"证据可核验"的地基）。"""
    _seed_task(session)
    service = _build(session, storage_root, _FakeReadGateway())

    result = service.download_contract_attachment("HT-2026-0001", "A-1")
    session.commit()

    stored = LocalFileStorage(storage_root / "objects").get(result.object_key)
    assert stored == PDF_BYTES


def test_materialized_path_is_readable_and_relative(
    session: Session, storage_root: Path
) -> None:
    """`file_path` 是**相对 storage_root** 的受控物化路径，且文件确实存在。"""
    _seed_task(session)
    service = _build(session, storage_root, _FakeReadGateway())

    result = service.download_contract_attachment("HT-2026-0001", "A-1")
    session.commit()

    assert not result.file_path.startswith("/")
    assert result.file_path.startswith("workspace/")
    assert (storage_root / result.file_path).read_bytes() == PDF_BYTES

    record = _attachment(session)
    assert record.file_path == result.file_path


def test_download_creates_attachment_record_when_detail_not_synced(
    session: Session, storage_root: Path
) -> None:
    """没同步详情也能直接下载：按下载结果补齐附件元数据。

    工具 3 是独立可调用的，不该强制调用方先跑一遍工具 2。
    """
    _seed_task(session)
    session.execute(select(ApprovalAttachment))  # 确保此刻确实没有附件记录
    service = _build(session, storage_root, _FakeReadGateway())

    result = service.download_contract_attachment("HT-2026-0001", "A-1")
    session.commit()

    record = _attachment(session)
    assert record.attachment_id == "A-1"
    assert record.file_name == "contract.pdf"
    assert record.file_type == "pdf"
    assert result.file_name == "contract.pdf"


def test_filename_override_is_used(
    session: Session, storage_root: Path
) -> None:
    """工具签名的 `file_name` 参数（可选）可覆盖外部返回的文件名。"""
    _seed_task(session)
    service = _build(session, storage_root, _FakeReadGateway())

    result = service.download_contract_attachment(
        "HT-2026-0001", "A-1", file_name="采购合同-扫描件.pdf"
    )
    session.commit()

    assert result.file_name == "采购合同-扫描件.pdf"
    assert _attachment(session).file_name == "采购合同-扫描件.pdf"


# ============================================================
# 2. 任务不存在
# ============================================================


def test_missing_task_raises_task_not_found(
    session: Session, storage_root: Path
) -> None:
    """本系统没有这条任务 → `TaskNotFound`，提示"先拉取或同步详情"。

    刻意与 `INSTANCE_NOT_FOUND` 区分：那是"外部系统说没有"，
    这是"我们这边还没有" —— 排查方向完全不同。
    """
    service = _build(session, storage_root, _FakeReadGateway())

    with pytest.raises(TaskNotFound) as excinfo:
        service.download_contract_attachment("HT-NOT-PULLED", "A-1")

    assert excinfo.value.code == ErrorCode.TASK_NOT_FOUND
    assert excinfo.value.retryable is False


@pytest.mark.parametrize(
    ("instance_id", "attachment_id"),
    [("", "A-1"), ("   ", "A-1"), ("HT-1", ""), ("HT-1", "  ")],
    ids=["实例号空", "实例号空白", "附件号空", "附件号空白"],
)
def test_blank_parameters_are_rejected(
    session: Session, storage_root: Path, instance_id: str, attachment_id: str
) -> None:
    """空参数是编程错误，抛 `ValueError`，且不发出任何下载请求。"""
    gateway = _FakeReadGateway()
    service = _build(session, storage_root, gateway)

    with pytest.raises(ValueError):
        service.download_contract_attachment(instance_id, attachment_id)

    assert gateway.calls == 0


# ============================================================
# 3. 确定性校验失败 → 立即阻塞
# ============================================================


@pytest.mark.parametrize(
    ("content", "file_name", "content_type", "max_bytes", "expected_code"),
    [
        (
            b"",
            "empty.pdf",
            "application/pdf",
            1024 * 1024,
            ErrorCode.ATTACHMENT_EMPTY,
        ),
        (
            PDF_BYTES,
            "huge.pdf",
            "application/pdf",
            10,  # 把上限压小，"超限"用例就不需要一份大文件
            ErrorCode.ATTACHMENT_TOO_LARGE,
        ),
        (
            PDF_BYTES,
            "malware.exe",
            "application/x-msdownload",
            1024 * 1024,
            ErrorCode.ATTACHMENT_TYPE_NOT_ALLOWED,
        ),
    ],
    ids=["空文件", "超过大小上限", "类型不在白名单"],
)
def test_validation_failures_block_task_immediately(
    session: Session,
    storage_root: Path,
    content: bytes,
    file_name: str,
    content_type: str,
    max_bytes: int,
    expected_code: ErrorCode,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """校验失败是**确定性**错误：重试只会再次下载同一份不合规的附件。

    因此必须**立刻** `blocked`，不浪费重试次数。

    每个用例自带 `max_bytes`，而不是把所有用例的上限一起调小 ——
    否则"类型不在白名单"会先撞上大小上限，测的就不是它自己那条规则了。
    """
    monkeypatch.setattr(settings, "attachment_max_bytes", max_bytes)

    _seed_task(session)
    gateway = _FakeReadGateway(
        content=content, file_name=file_name, content_type=content_type
    )
    service = _build(session, storage_root, gateway)

    with pytest.raises(AttachmentValidationError) as excinfo:
        service.download_contract_attachment("HT-2026-0001", "A-1")
    session.commit()

    assert excinfo.value.code == expected_code
    assert excinfo.value.retryable is False

    task = _task(session)
    assert task.task_status == TaskStatus.BLOCKED.value
    assert task.blocked_stage == "download"
    assert task.last_error_code == expected_code.value
    assert task.block_reason

    job = _jobs(session)[0]
    assert job.job_status == JobStatus.FAILED.value
    assert job.next_retry_at is None
    # 未触发下载的校验失败不该落库任何附件
    assert session.execute(select(ApprovalAttachment)).scalars().all() == []


def test_attachment_missing_blocks_task_with_dedicated_code(
    session: Session, storage_root: Path
) -> None:
    """附件在外部系统中已被删除 → `ATTACHMENT_MISSING` + 立即阻塞。"""
    _seed_task(session)
    gateway = _FakeReadGateway(
        error=PermanentGatewayError(
            "附件 A-5002 在审批系统中已被删除", code=ErrorCode.ATTACHMENT_MISSING
        )
    )
    service = _build(session, storage_root, gateway)

    with pytest.raises(PermanentGatewayError):
        service.download_contract_attachment("HT-2026-0001", "A-5002")
    session.commit()

    task = _task(session)
    assert task.task_status == TaskStatus.BLOCKED.value
    assert task.blocked_stage == "download"
    assert task.last_error_code == ErrorCode.ATTACHMENT_MISSING.value


# ============================================================
# 附件记录的下载状态（验收标准 9）
# ============================================================


def _seed_attachment(
    session: Session, task: ApprovalTask, attachment_id: str = "A-5002"
) -> ApprovalAttachment:
    """造一条"详情同步时已发现"的附件记录。"""
    record = ApprovalAttachment(
        task_id=task.id,
        attachment_id=attachment_id,
        file_name=f"{attachment_id}.pdf",
        download_status=DownloadStatus.PENDING.value,
    )
    session.add(record)
    session.commit()
    return record


def test_permanent_failure_marks_the_attachment_failed(
    session: Session, storage_root: Path
) -> None:
    """**验收 9**：确定性失败后，附件记录必须变成 `failed`。

    `pending` 的语义是"排队中，稍后会做"。一份确定性失败的附件
    若一直停在 `pending`，控制台会显示"待下载"，而它其实在等人处理 ——
    **状态与事实不符**是最难排查的一类问题：看板显示一切正常，任务却永远不动。
    """
    task = _seed_task(session)
    _seed_attachment(session, task, "A-5002")

    gateway = _FakeReadGateway(
        error=PermanentGatewayError(
            "附件 A-5002 在审批系统中已被删除", code=ErrorCode.ATTACHMENT_MISSING
        )
    )
    service = _build(session, storage_root, gateway)

    with pytest.raises(PermanentGatewayError):
        service.download_contract_attachment("HT-2026-0001", "A-5002")
    session.commit()

    record = _attachment(session)
    assert record.download_status == DownloadStatus.FAILED.value
    assert record.error_message and "已被删除" in record.error_message


def test_transient_failure_keeps_the_attachment_pending(
    session: Session, storage_root: Path
) -> None:
    """还会重试时，附件**必须留在 `pending`**。

    此时 `pending` 才是准确描述 —— 它确实还在队列里，退避到点就会重试。
    提前标 `failed` 会让这次重试看起来"从未发生过"，
    而重试真的发生并成功时，状态又变回 `success`，中间留下一次误导。
    """
    task = _seed_task(session)
    _seed_attachment(session, task, "A-5002")

    gateway = _FakeReadGateway(
        error=TransientGatewayError("超时", code=ErrorCode.APPROVAL_API_TIMEOUT)
    )
    service = _build(session, storage_root, gateway)

    with pytest.raises(TransientGatewayError):
        service.download_contract_attachment("HT-2026-0001", "A-5002")
    session.commit()

    assert _attachment(session).download_status == DownloadStatus.PENDING.value
    assert _jobs(session)[0].job_status == JobStatus.RETRY_WAIT.value


def test_failure_without_existing_record_creates_no_placeholder(
    session: Session, storage_root: Path
) -> None:
    """记录不存在时**不得凭空新建**占位行。

    没有记录说明这份附件从未被"详情同步"发现过。造一条只有编号、
    其他字段全空的记录，只会让附件列表多一行无意义的数据。

    失败信息也没有丢：任务的 `blocked_stage='download'` 与
    `last_error_code` 已经指明问题出在哪一步、是什么。
    """
    _seed_task(session)
    gateway = _FakeReadGateway(
        error=PermanentGatewayError(
            "附件 A-5002 在审批系统中已被删除", code=ErrorCode.ATTACHMENT_MISSING
        )
    )
    service = _build(session, storage_root, gateway)

    with pytest.raises(PermanentGatewayError):
        service.download_contract_attachment("HT-2026-0001", "A-5002")
    session.commit()

    assert session.execute(select(ApprovalAttachment)).scalars().all() == []
    assert _task(session).last_error_code == ErrorCode.ATTACHMENT_MISSING.value


def test_validation_error_writes_structured_log(
    session: Session, storage_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "attachment_max_bytes", 10)
    _seed_task(session)
    service = _build(session, storage_root, _FakeReadGateway())

    with pytest.raises(AttachmentValidationError):
        service.download_contract_attachment("HT-2026-0001", "A-1")
    session.commit()

    entry = next(entry for entry in _logs(session) if entry.log_level == "error")
    assert entry.error_code == ErrorCode.ATTACHMENT_TOO_LARGE.value
    assert entry.log_type == "download"


# ============================================================
# 4. 瞬时错误 → 先重试，耗尽才阻塞
# ============================================================


def test_transient_failure_does_not_block_task_while_retries_remain(
    session: Session, storage_root: Path
) -> None:
    """**瞬时错误在重试未耗尽前不得阻塞任务。**

    这是最容易被写错的一条：自动重试还没跑完就把任务打成 `blocked`，
    等于让一次网络抖动产生一个需要人工介入的失败任务。
    """
    _seed_task(session)
    gateway = _FakeReadGateway(
        error=TransientGatewayError("超时", code=ErrorCode.APPROVAL_API_TIMEOUT)
    )
    service = _build(session, storage_root, gateway)

    with pytest.raises(TransientGatewayError):
        service.download_contract_attachment("HT-2026-0001", "A-1")
    session.commit()

    job = _jobs(session)[0]
    assert job.job_status == JobStatus.RETRY_WAIT.value
    assert job.next_retry_at is not None
    assert job.attempt_no == 1

    task = _task(session)
    assert task.task_status != TaskStatus.BLOCKED.value, "重试还没跑完就阻塞了任务"
    assert task.blocked_stage is None


def test_transient_failure_blocks_task_only_after_retries_exhausted(
    session: Session, storage_root: Path
) -> None:
    """瞬时错误重试耗尽（默认 3 次）后才阻塞任务。"""
    _seed_task(session)
    gateway = _FakeReadGateway(
        error=TransientGatewayError("5xx", code=ErrorCode.APPROVAL_API_ERROR)
    )
    service = _build(session, storage_root, gateway)

    for attempt in range(1, 4):
        with pytest.raises(TransientGatewayError):
            service.download_contract_attachment("HT-2026-0001", "A-1")
        session.commit()

        task = _task(session)
        if attempt < 3:
            assert task.task_status != TaskStatus.BLOCKED.value, (
                f"第 {attempt} 次失败就阻塞了任务，重试预算被浪费"
            )
        else:
            assert task.task_status == TaskStatus.BLOCKED.value
            assert task.last_error_code == ErrorCode.APPROVAL_API_ERROR.value

    # 三次尝试累积在**同一条**作业上 —— 这正是一次性键做不到的
    assert len(_jobs(session)) == 1
    assert _jobs(session)[0].attempt_no == 3


def test_storage_transient_error_also_retries(
    session: Session, storage_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """存储"暂时不可用"同样是瞬时错误：重试通常就好了。"""
    _seed_task(session)
    service = _build(session, storage_root, _FakeReadGateway())

    def fail_put(*args: object, **kwargs: object) -> None:
        raise TransientStorageError("存储抖动", code=ErrorCode.STORAGE_UNAVAILABLE)

    monkeypatch.setattr(LocalFileStorage, "put", fail_put)

    with pytest.raises(TransientStorageError):
        service.download_contract_attachment("HT-2026-0001", "A-1")
    session.commit()

    assert _jobs(session)[0].job_status == JobStatus.RETRY_WAIT.value
    assert _task(session).task_status != TaskStatus.BLOCKED.value


def test_explicit_recall_after_exhaustion_starts_a_fresh_round(
    session: Session, storage_root: Path
) -> None:
    """上一轮重试耗尽后，再次**显式调用**应当拿回完整的重试预算。

    否则作业永远卡在 `failed`，人工怎么点都没用，只能手工改库。

    区分标准：跨越服务入口 = 新一轮；进程内自动重试 = 同一轮。
    """
    _seed_task(session)
    gateway = _FakeReadGateway(
        error=TransientGatewayError("5xx", code=ErrorCode.APPROVAL_API_ERROR)
    )
    service = _build(session, storage_root, gateway)

    for _ in range(3):
        with pytest.raises(TransientGatewayError):
            service.download_contract_attachment("HT-2026-0001", "A-1")
        session.commit()
    assert _jobs(session)[0].job_status == JobStatus.FAILED.value

    # 第 4 次显式调用 → 作业重置，attempt_no 从 1 重新开始
    with pytest.raises(TransientGatewayError):
        service.download_contract_attachment("HT-2026-0001", "A-1")
    session.commit()

    job = _jobs(session)[0]
    assert job.attempt_no == 1, "显式重新调用没有拿回重试预算"
    assert job.job_status == JobStatus.RETRY_WAIT.value


# ============================================================
# 5. 阻塞后恢复
# ============================================================


def test_successful_download_unblocks_task_stuck_on_download_stage(
    session: Session, storage_root: Path
) -> None:
    """任务正卡在"下载"阶段时，下载成功必须**解除阻塞**。

    否则任务会永远停在 `blocked`，而附件明明已经下好了 ——
    这种"状态与事实不符"最难排查：所有数据都对，只有状态是错的。
    """
    _seed_task(
        session,
        task_status=TaskStatus.BLOCKED.value,
        blocked_stage="download",
        last_error_code=ErrorCode.APPROVAL_API_TIMEOUT.value,
        block_reason="下载超时",
    )
    service = _build(session, storage_root, _FakeReadGateway())

    service.download_contract_attachment("HT-2026-0001", "A-1")
    session.commit()

    task = _task(session)
    assert task.task_status == TaskStatus.PARSING.value, "应从检查点恢复到解析阶段"
    assert task.blocked_stage is None
    assert task.last_error_code is None
    assert task.block_reason is None
    assert task.retry_count == 1, "从阻塞恢复应当计入重试次数"


def test_successful_download_does_not_touch_other_blocked_stages(
    session: Session, storage_root: Path
) -> None:
    """任务卡在**别的**阶段时，下载成功不该顺手解除阻塞。

    例如规则阶段失败的任务，下载成功与"规则能不能跑"毫无关系；
    顺手 un-block 会把一个真实的失败掩盖掉。
    """
    _seed_task(
        session,
        task_status=TaskStatus.BLOCKED.value,
        blocked_stage="rule",
        last_error_code=ErrorCode.APPROVAL_API_ERROR.value,
        block_reason="规则阶段失败",
    )
    service = _build(session, storage_root, _FakeReadGateway())

    service.download_contract_attachment("HT-2026-0001", "A-1")
    session.commit()

    task = _task(session)
    assert task.task_status == TaskStatus.BLOCKED.value
    assert task.blocked_stage == "rule"
    assert task.retry_count == 0


# ============================================================
# 6. 不可信输入：文件名
# ============================================================


@pytest.mark.parametrize(
    ("raw_name", "expected"),
    [
        ("../../etc/passwd", "passwd"),
        ("..\\..\\windows\\system32\\config", "config"),
        ("/absolute/path/合同.pdf", "合同.pdf"),
        ("....", "A-1.pdf"),
        ("...", "A-1.pdf"),
        ("", "A-1.pdf"),
        ("   ", "A-1.pdf"),
        ("nor/mal.pdf", "mal.pdf"),
    ],
    ids=[
        "上跳多级",
        "反斜杠穿越",
        "绝对路径",
        "四个点",
        "三个点",
        "空串",
        "纯空白",
        "含斜杠",
    ],
)
def test_malicious_file_names_are_sanitized(
    session: Session,
    storage_root: Path,
    raw_name: str,
    expected: str,
) -> None:
    """文件名来自外部系统的 `Content-Disposition`，必须当作攻击者可影响的输入。

    直接拼进路径可以构造目录穿越 —— 因此先彻底清洗，
    再校验最终路径落在工作目录内（两层防护）。
    """
    _seed_task(session)
    gateway = _FakeReadGateway(file_name=raw_name)
    service = _build(session, storage_root, gateway)

    result = service.download_contract_attachment("HT-2026-0001", "A-1")
    session.commit()

    assert result.file_name == expected
    assert ".." not in result.file_name
    assert "/" not in result.file_name

    # 物化路径必须仍在工作目录内
    materialized = (storage_root / result.file_path).resolve()
    assert materialized.is_relative_to((storage_root / "workspace").resolve())
    assert materialized.exists()


def test_materialized_file_stays_inside_workspace(
    session: Session, storage_root: Path
) -> None:
    """穿越尝试不得在工作目录之外留下任何文件。"""
    _seed_task(session)
    service = _build(
        session, storage_root, _FakeReadGateway(file_name="../../escaped.pdf")
    )

    result = service.download_contract_attachment("HT-2026-0001", "A-1")
    session.commit()

    assert not (storage_root / "escaped.pdf").exists()
    assert (storage_root / result.file_path).exists()


def test_approval_code_is_sanitized_in_workspace_path(
    session: Session, storage_root: Path
) -> None:
    """审批编号也会进路径，同样要清洗。"""
    _seed_task(session, instance_id="HT/../evil")
    service = _build(session, storage_root, _FakeReadGateway())

    result = service.download_contract_attachment("HT/../evil", "A-1")
    session.commit()

    assert ".." not in result.file_path
    assert (storage_root / result.file_path).exists()


# ============================================================
# 7. 内容类型推断
# ============================================================


def test_octet_stream_falls_back_to_extension(
    session: Session, storage_root: Path
) -> None:
    """响应头只给了 `application/octet-stream` 时，按扩展名推断类型。

    真实系统未必都正确设置 `Content-Type`，一律拒绝会让可用性无谓变差。
    """
    _seed_task(session)
    gateway = _FakeReadGateway(content_type="application/octet-stream")
    service = _build(session, storage_root, gateway)

    result = service.download_contract_attachment("HT-2026-0001", "A-1")
    session.commit()

    assert result.content_type == "application/pdf"


def test_unknown_extension_still_rejected(
    session: Session, storage_root: Path
) -> None:
    """扩展名不认识时仍按原类型拒绝 —— 推断必须是**保守**的。"""
    _seed_task(session)
    gateway = _FakeReadGateway(
        file_name="payload.bin", content_type="application/octet-stream"
    )
    service = _build(session, storage_root, gateway)

    with pytest.raises(AttachmentValidationError) as excinfo:
        service.download_contract_attachment("HT-2026-0001", "A-1")

    assert excinfo.value.code == ErrorCode.ATTACHMENT_TYPE_NOT_ALLOWED


# ============================================================
# 8. 日志
# ============================================================


def test_success_log_records_object_key(
    session: Session, storage_root: Path
) -> None:
    """成功日志带对象键与摘要，便于把日志与对象、数据库记录对上。"""
    _seed_task(session)
    service = _build(session, storage_root, _FakeReadGateway())

    result = service.download_contract_attachment("HT-2026-0001", "A-1")
    session.commit()

    entry = next(entry for entry in _logs(session) if entry.log_type == "download")
    assert entry.error_code is None
    assert result.object_key in entry.log_content


def test_retry_log_is_warning_with_code(
    session: Session, storage_root: Path
) -> None:
    """"还会重试"记 warning（不是 error），但**仍带错误码** —— 它可统计。"""
    _seed_task(session)
    gateway = _FakeReadGateway(
        error=TransientGatewayError("超时", code=ErrorCode.APPROVAL_API_TIMEOUT)
    )
    service = _build(session, storage_root, gateway)

    with pytest.raises(TransientGatewayError):
        service.download_contract_attachment("HT-2026-0001", "A-1")
    session.commit()

    entry = next(entry for entry in _logs(session) if entry.log_type == "download")
    assert entry.log_level == "warning"
    assert entry.error_code == ErrorCode.APPROVAL_API_TIMEOUT.value


# ============================================================
# 作业键的租户维度 与 附件编号的传递（复审修正）
# ============================================================


def test_download_job_key_includes_tenant(
    session: Session, storage_root: Path
) -> None:
    """**两个租户下相同的实例号 + 附件号必须产生两条作业，且各挂各的任务。**

    回归"下载作业幂等键缺少租户维度"。缺了它，键会退化成
    `download:SAME:ATT-1:first`，第二个租户直接命中第一个租户已建的作业：

    ```text
    tenant-a 建作业（task_id = a）
    tenant-b 命中同一个键 → 复用 a 的作业
            → 自己的下载失败改写 a 的作业记录
            → 而 job.task_id 仍指向 a 的任务
    ```

    这就是跨租户作业状态串写。拉取作业与详情作业的 identity 都含
    `provider:tenant`，下载作业必须一致——三处不一致本身就是隐患。
    """
    task_a = _seed_task(session, "SAME", tenant_id="tenant-a")
    task_b = _seed_task(session, "SAME", tenant_id="tenant-b")
    session.commit()

    gateway_a = _FakeReadGateway()
    gateway_a.tenant_id = "tenant-a"  # 端口成员：服务据此定位任务并构造幂等键
    gateway_b = _FakeReadGateway()
    gateway_b.tenant_id = "tenant-b"

    _build(session, storage_root, gateway_a).download_contract_attachment("SAME", "ATT-1")
    session.commit()
    _build(session, storage_root, gateway_b).download_contract_attachment("SAME", "ATT-1")
    session.commit()

    jobs = _jobs(session)
    assert len(jobs) == 2, "两个租户必须各有一条下载作业"
    assert len({job.idempotency_key for job in jobs}) == 2, "幂等键不得相同"
    assert {job.task_id for job in jobs} == {task_a.id, task_b.id}, (
        "作业必须各挂各的任务，不得跨租户引用"
    )


def test_attachment_id_with_colon_is_recorded_verbatim(
    session: Session, storage_root: Path
) -> None:
    """附件编号**含冒号**时，失败记录必须指向真正的那一份附件。

    回归"从幂等键反解析附件编号"。附件编号没有字符限制
    （`ATT:1` 合法），而反解析依赖"键里只有一层冒号分隔"这个不成立的假设：

    ```text
    download:mock:default:SAME:ATT:1:first
                                     ↑ rsplit 取到的是 "1"，不是 "ATT:1"
    ```

    后果很具体：任务被正确阻塞，而**附件记录永远留在 `pending`** ——
    控制台显示"待下载"，实际它在等人处理；且日志里记的编号是错的，
    排障时照着去查会查到一份不存在的附件。
    """
    task = _seed_task(session)
    _seed_attachment(session, task, "ATT:1")

    gateway = _FakeReadGateway(
        error=PermanentGatewayError(
            "附件 ATT:1 在审批系统中已被删除", code=ErrorCode.ATTACHMENT_MISSING
        )
    )
    service = _build(session, storage_root, gateway)

    with pytest.raises(PermanentGatewayError):
        service.download_contract_attachment("HT-2026-0001", "ATT:1")
    session.commit()

    record = _attachment(session)
    assert record.attachment_id == "ATT:1"
    assert record.download_status == DownloadStatus.FAILED.value
    assert record.error_message

    entry = next(entry for entry in _logs(session) if entry.log_level == "error")
    assert "ATT:1" in entry.log_content, "日志里的附件编号必须是原始值，不能是被截断的片段"
