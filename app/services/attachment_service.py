"""附件模块 —— 下载、校验、落盘与元数据（工具 3 的业务实现，企业化设计 §5.2）。

两种"位置"必须分清：`object_key` 是**长期**保存位置（只有本系统用），
`file_path` 是**受控临时物化路径**（供解析工具读取）。合并成一个字段，
就等于让调用端拿到一份可绕过鉴权的永久路径。需求要求工具 3 返回 `file_path`，
所以它保留 —— 但它**不是**长期位置。

失败处理：确定性错误（类型 / 超限 / 空文件 / 附件已删除）→ 作业 `failed`、
任务**立即 `blocked`**；瞬时错误（超时 / 5xx / 连不上 / 存储暂不可用）→
`retry_wait`、**暂不阻塞**。

"暂不阻塞"常被写错：自动重试还没跑完就打成 `blocked`，
等于让一次网络抖动产生一个需要人工介入的失败任务。

文件名来自外部系统（`Content-Disposition`），是**攻击者可影响**的输入：
直接拼进路径可构造目录穿越，因此先彻底清洗名字，再校验最终路径落在工作目录内。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.enums import (
    DownloadStatus,
    ErrorCode,
    JobStatus,
    JobType,
    LogLevel,
    LogType,
    TaskStatus,
)
from app.errors import (
    AppError,
    AttachmentValidationError,
    PermanentStorageError,
    TaskNotFound,
)
from app.models import ApprovalAttachment, ApprovalTask
from app.ports.approval_gateway import ApprovalReadGateway, DownloadedAttachmentDTO
from app.ports.object_storage import ObjectStorage, content_addressed_key
from app.services.dto import DownloadResult
from app.services.log_service import LogService
from app.workflow.jobs import (
    build_idempotency_key,
    create_job,
    mark_failed,
    mark_running,
    mark_succeeded,
    reset_for_retry,
)
from app.workflow.state_machine import mark_blocked, start_retry

#: 文件系统不接受的字符（含 Windows 保留字符与控制字符）
_UNSAFE_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

#: 文件名长度上限（字节级限制之外，也给日志与界面留可读空间）
_MAX_NAME_LENGTH = 120

#: 响应头没给（或只给了 `application/octet-stream`）时，按扩展名推断类型。
#: 真实系统未必都会正确设置 Content-Type，一律拒绝会让可用性无谓变差。
_EXTENSION_CONTENT_TYPES: dict[str, str] = {
    ".pdf": "application/pdf",
}

#: 没下载过时用的固定版本标记。
#: 它的作用是让**自动重试累积在同一个作业上** —— 若每次都用一次性键，
#: `max_attempts` 永远不会耗尽，"重试耗尽后 blocked"这条规则就失效了。
_FIRST_DOWNLOAD_VERSION = "first"


@dataclass(frozen=True)
class _StoredBlob:
    """落盘结果：对象存储引用 + 物化路径。"""

    object_key: str
    sha256: str
    size: int
    content_type: str
    #: 相对 `storage_root` 的物化路径
    materialized_path: str


class AttachmentService:
    """附件模块的应用服务。"""

    def __init__(
        self,
        gateway: ApprovalReadGateway,
        storage: ObjectStorage,
        session: Session,
        *,
        log: LogService | None = None,
    ) -> None:
        """`gateway` 提供下载能力（下载也是读取能力的一部分）；
        `storage` 落盘（M3 为本地实现，M9 换 MinIO 语义不变）。
        `session` 的提交由调用方负责；`log` 缺省用同一会话新建。
        """
        self._gateway = gateway
        self._storage = storage
        self._session = session
        self._log = log or LogService(session)
        self._provider = gateway.provider
        self._tenant_id = gateway.tenant_id

    # ------------------------------------------------------------------
    # 工具 3
    # ------------------------------------------------------------------
    def download_contract_attachment(
        self,
        instance_id: str,
        attachment_id: str,
        file_name: str | None = None,
    ) -> DownloadResult:
        """下载合同附件：校验 → SHA-256 → 对象存储 → 物化 → 更新附件记录。

        `file_name` 不传则用外部系统返回的名字。

        Raises:
            TaskNotFound: 本系统里没有这条任务 —— 需先拉取待办或同步详情。
            AppError: 下载或存储失败（已按可重试性更新作业与任务状态）。
            ValueError: 参数为空（编程错误）。
        """
        instance_key = _require_text(instance_id, field="instance_id")
        attachment_key = _require_text(attachment_id, field="attachment_id")

        task = self._require_task(instance_key)
        existing = self._find_attachment(task.id, attachment_key)

        job = self._open_download_job(
            task, instance_key, attachment_key, existing
        )
        mark_running(self._session, job)

        try:
            downloaded = self._gateway.download_attachment(
                instance_key, attachment_key
            )
            self._validate(downloaded)
            # 文件名在这里**只算一次**，物化路径与数据库记录共用同一个值。
            # 若两处各算一次，兜底规则稍有差异就会出现
            # "磁盘上叫 A.pdf、数据库里写 B.pdf" 这种对不上且难查的状态。
            safe_name = _safe_file_name(
                file_name or downloaded.file_name,
                fallback=f"{attachment_key}.pdf",
            )
            stored = self._store(task, downloaded, safe_name)
        except AppError as exc:
            self._handle_failure(task, job, attachment_key, exc)
            raise

        mark_succeeded(self._session, job)
        record = self._persist_attachment(
            task, existing, attachment_key, downloaded, stored, safe_name
        )

        # 若任务正是卡在"下载"阶段，本次成功就等于完成了那个失败的步骤 ——
        # **必须从检查点恢复**。否则任务会永远停在 blocked，
        # 而附件明明已经下好了；这种"状态与事实不符"最难排查。
        if (
            task.task_status == TaskStatus.BLOCKED.value
            and task.blocked_stage == JobType.DOWNLOAD.value
        ):
            resumed = start_retry(task)
            self._log.log(
                log_type=LogType.DOWNLOAD,
                message=f"下载成功后从检查点恢复任务：blocked → {resumed}",
                task_id=task.id,
            )

        self._log.log(
            log_type=LogType.DOWNLOAD,
            message=(
                f"附件下载完成：{record.file_name}"
                f"（{stored.size} 字节，sha256={stored.sha256[:12]}…）"
            ),
            task_id=task.id,
            payload={
                "attachment_id": attachment_key,
                "object_key": stored.object_key,
                "content_type": stored.content_type,
            },
        )
        self._session.flush()

        return DownloadResult(
            task_id=task.id,
            attachment_id=attachment_key,
            file_name=record.file_name,
            file_size=stored.size,
            file_checksum=stored.sha256,
            object_key=stored.object_key,
            file_path=stored.materialized_path,
            content_type=stored.content_type,
            download_status=record.download_status,
        )

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------
    def _validate(self, downloaded: DownloadedAttachmentDTO) -> None:
        """校验下载结果：非空 → 类型白名单 → 大小上限。

        Raises:
            AttachmentValidationError: 任一校验不通过。

        全部是**确定性**错误：重试只会再次下载同一份不合规的附件，
        因此调用方应立即让任务 `blocked`，不浪费重试次数。
        """
        content = downloaded.content

        if not content:
            raise AttachmentValidationError(
                f"附件 {downloaded.file_name} 内容为空",
                code=ErrorCode.ATTACHMENT_EMPTY,
            )

        size = len(content)
        if size > settings.attachment_max_bytes:
            raise AttachmentValidationError(
                f"附件 {downloaded.file_name} 大小 {size} 字节，"
                f"超过上限 {settings.attachment_max_bytes} 字节",
                code=ErrorCode.ATTACHMENT_TOO_LARGE,
            )

        content_type = _effective_content_type(
            downloaded.content_type, downloaded.file_name
        )
        allowed = settings.allowed_attachment_types
        if content_type not in allowed:
            raise AttachmentValidationError(
                f"附件类型 {content_type}（来自 {downloaded.file_name}）不在白名单内："
                f"允许 {', '.join(allowed)}",
                code=ErrorCode.ATTACHMENT_TYPE_NOT_ALLOWED,
            )

    # ------------------------------------------------------------------
    # 落盘
    # ------------------------------------------------------------------
    def _store(
        self,
        task: ApprovalTask,
        downloaded: DownloadedAttachmentDTO,
        safe_name: str,
    ) -> _StoredBlob:
        """写入对象存储，并在受控工作目录中物化一份。

        `safe_name` 由调用方传入（只算一次），保证物化路径与数据库记录同名。

        Raises:
            TransientStorageError / PermanentStorageError: 由存储适配器给出，
            两者都已带稳定错误码，可直接用于作业分类。
        """
        digest = hashlib.sha256(downloaded.content).hexdigest()
        content_type = _effective_content_type(
            downloaded.content_type, downloaded.file_name
        )

        suffix = PurePosixPath(safe_name).suffix.lstrip(".") or "bin"
        object_key = content_addressed_key(digest, suffix=suffix)
        reference = self._storage.put(
            object_key, downloaded.content, content_type=content_type
        )

        materialized = self._materialize(
            task.approval_code or task.instance_id, safe_name, downloaded.content
        )

        return _StoredBlob(
            object_key=reference.key,
            sha256=reference.sha256,
            size=reference.size,
            content_type=content_type,
            materialized_path=materialized,
        )

    def _materialize(self, approval_code: str, file_name: str, content: bytes) -> str:
        """把附件写到受控工作目录，返回**相对 `storage_root`** 的路径。

        为什么要有这一步：需求要求工具 3 返回 `file_path` 供后续解析使用。
        对象存储的键不能直接当文件路径用（M9 换成 MinIO 后根本没有本地文件），
        因此这里显式物化一份到工作目录。

        落点：`{storage_root}/workspace/{approval_code}/{file_name}`
        """
        root = settings.storage_path
        workspace = _resolve_under(root, "workspace", _safe_segment(approval_code))
        workspace.mkdir(parents=True, exist_ok=True)

        target = _resolve_under(workspace, file_name)
        try:
            target.write_bytes(content)
        except OSError as exc:
            raise PermanentStorageError(
                f"物化附件失败：{target.name}", code=ErrorCode.STORAGE_WRITE_DENIED
            ) from exc

        return target.relative_to(root).as_posix()

    # ------------------------------------------------------------------
    # 附件记录
    # ------------------------------------------------------------------
    def _persist_attachment(
        self,
        task: ApprovalTask,
        existing: ApprovalAttachment | None,
        attachment_id: str,
        downloaded: DownloadedAttachmentDTO,
        stored: _StoredBlob,
        safe_name: str,
    ) -> ApprovalAttachment:
        """写入/更新附件记录。

        记录不存在时**新建**：工具 3 允许在未同步详情的情况下被直接调用
        （前提是任务已存在），此时用下载结果补齐元数据。

        `safe_name` 由调用方传入，与物化路径**同名** —— 两处各算一次会产生
        "磁盘上叫 A.pdf、数据库里写 B.pdf"这种对不上且难查的状态。
        """
        display_name = safe_name

        if existing is None:
            existing = ApprovalAttachment(
                task_id=task.id,
                attachment_id=attachment_id,
                file_name=display_name,
            )
            self._session.add(existing)

        existing.file_name = display_name
        existing.file_type = PurePosixPath(display_name).suffix.lstrip(".") or None
        existing.content_type = stored.content_type
        existing.object_key = stored.object_key
        existing.file_path = stored.materialized_path
        existing.file_size = stored.size
        existing.file_checksum = stored.sha256
        existing.download_status = DownloadStatus.SUCCESS.value
        # 下载成功后清掉历史失败原因，否则界面会显示一个已经不成立的错误
        existing.error_message = None

        self._session.flush()
        return existing

    def _require_task(self, instance_id: str) -> ApprovalTask:
        """按实例号取出本系统内的任务。

        Raises:
            TaskNotFound: 本系统还没有这条任务。
        """
        statement = select(ApprovalTask).where(
            ApprovalTask.provider == self._provider,
            ApprovalTask.tenant_id == self._tenant_id,
            ApprovalTask.instance_id == instance_id,
        )
        task = self._session.execute(statement).scalar_one_or_none()
        if task is None:
            raise TaskNotFound(
                f"系统中没有审批单 {instance_id} 的任务记录："
                f"请先拉取待办（工具 1）或同步详情（工具 2）",
                code=ErrorCode.TASK_NOT_FOUND,
            )
        return task

    def _find_attachment(
        self, task_id: int, attachment_id: str
    ) -> ApprovalAttachment | None:
        statement = select(ApprovalAttachment).where(
            ApprovalAttachment.task_id == task_id,
            ApprovalAttachment.attachment_id == attachment_id,
        )
        return self._session.execute(statement).scalar_one_or_none()

    # ------------------------------------------------------------------
    # 作业
    # ------------------------------------------------------------------
    def _open_download_job(
        self,
        task: ApprovalTask,
        instance_id: str,
        attachment_id: str,
        existing: ApprovalAttachment | None,
    ):
        """创建/复用下载作业。

        **版本取"已下载内容摘要"或固定标记，而不是一次性键**：
        这样自动重试会累积在同一个作业上，`max_attempts` 才能真正耗尽并触发
        `blocked`。用一次性键的话每次调用都是全新作业，
        "重试耗尽"永远到不了。

        作业已 `failed`（上一轮重试耗尽）时**显式重置**：
        服务入口被调用 = 新一轮，理应拿回完整的重试预算。
        Worker 内部的自动重试不走这里，因此不受影响。
        """
        version = (
            existing.file_checksum
            if existing is not None and existing.file_checksum
            else _FIRST_DOWNLOAD_VERSION
        )
        # ⚠️ identity 必须含 provider + tenant_id。
        # 少了它们，两个租户下相同的实例号 + 附件号会**算出同一个键**：
        #   tenant-a: download:A:1:first   ← 先建
        #   tenant-b: download:A:1:first   ← 命中幂等键，复用 a 的作业
        # 于是 b 的下载失败会改写 a 的作业记录，且作业仍挂在 a 的任务上 ——
        # 跨租户作业状态串写，与"为将来接入企业预留租户维度"的目标直接冲突。
        # 拉取作业与详情作业都带 `provider:tenant`，这里保持一致。
        identity = f"{self._provider}:{self._tenant_id}:{instance_id}:{attachment_id}"
        key = build_idempotency_key(JobType.DOWNLOAD, identity, version)
        job, created = create_job(
            self._session,
            job_type=JobType.DOWNLOAD,
            idempotency_key=key,
            input_payload={
                "provider": self._provider,
                "tenant_id": self._tenant_id,
                "instance_id": instance_id,
                "attachment_id": attachment_id,
                # ⚠️ 首次下载时附件记录**还不存在**（记录在下载成功后才建），
                # 因此这一项如实为 None —— 不能填一个事后才成立的编号，
                # 那会让"这份结果基于什么输入"记录下一个当时不存在的值。
                "attachment_record_id": None if existing is None else existing.id,
            },
            task_id=task.id,
        )
        if not created and job.job_status == JobStatus.FAILED.value:
            reset_for_retry(self._session, job)
        return job

    def _handle_failure(
        self,
        task: ApprovalTask,
        job,
        attachment_id: str,
        error: AppError,
    ) -> None:
        """失败收尾：更新作业，并**只在重试无望时**才阻塞任务。

        `attachment_id` 由调用方**显式传入**，不从幂等键反解析 ——
        见下方 payload 处的说明。
        """
        status = mark_failed(self._session, job, error=error)

        # ⚠️ `attachment_id` 由调用方传入，**不从幂等键反解析**：附件编号没有任何
        # 字符限制（`ATT:1` 是合法的），反解析依赖"键里只有一层冒号"这个不成立的
        # 假设 —— `download:...:ATT:1:first` 会 rsplit 出 "1"。后果具体：任务被正确
        # 阻塞，而附件记录永远留在 pending，两者状态不一致且看不出是哪一份附件。
        payload = {
            "attachment_id": attachment_id,
            "job_status": status.value,
            "attempt_no": job.attempt_no,
            "max_attempts": job.max_attempts,
            "next_retry_at": None if job.next_retry_at is None else job.next_retry_at.isoformat(),
        }

        if status is JobStatus.FAILED:
            # 重试无望（确定性错误，或瞬时错误已耗尽次数）→ 才让任务阻塞
            mark_blocked(
                task,
                stage=JobType.DOWNLOAD,
                error_code=error.code,
                message=error.message,
            )
            self._mark_attachment_failed(task, attachment_id, error)
            self._log.log_error(
                log_type=LogType.DOWNLOAD,
                message=f"附件下载失败并阻塞任务：{error.message}",
                error_code=error.code,
                task_id=task.id,
                payload=payload,
            )
            return

        # 还会重试：**任务暂不阻塞**。
        # 自动重试还没跑完就打成 blocked，会让一次网络抖动
        # 产生一个需要人工介入的失败任务。
        self._log.log(
            log_type=LogType.DOWNLOAD,
            level=LogLevel.WARNING,
            message=f"附件下载失败，将按退避重试：{error.message}",
            error_code=error.code,
            task_id=task.id,
            payload=payload,
        )

    def _mark_attachment_failed(
        self, task: ApprovalTask, attachment_id: str, error: AppError
    ) -> None:
        """把附件记录标成 `failed`（**只在确定性失败时**调用）。

        `pending` 的语义是"排队中，稍后会做"。确定性失败的附件若一直保持 `pending`，
        控制台会显示"待下载"，而它实际上在等人处理 —— **状态与事实不符**是最难排查的
        一类问题：看板显示一切正常，任务却永远不动。

        两个边界：**记录不存在时不动** —— 没有记录说明该附件从未被详情同步发现过，
        凭空建一条只有编号的占位行只会多一行无意义数据，而失败信息也没丢：
        任务的 `blocked_stage='download'` 与 `last_error_code` 已指明问题出在哪一步；
        **"还会重试"的失败也不动** —— 那时留在 `pending` 才准确，它确实还在队列里，
        提前标 `failed` 会让这次重试看起来没发生过。
        """
        record = self._find_attachment(task.id, attachment_id)
        if record is None:
            return
        record.download_status = DownloadStatus.FAILED.value
        record.error_message = error.message


# ============================================================
# 内部工具
# ============================================================


def _require_text(value: str, *, field: str) -> str:
    """必填文本参数：非空且不含首尾空白。"""
    text = (value or "").strip()
    if not text:
        raise ValueError(f"{field} 不能为空")
    return text


def _effective_content_type(raw: str, file_name: str) -> str:
    """确定用于白名单校验的类型。

    响应头没给、或只给了 `application/octet-stream` 时，回退到按扩展名推断。
    真实系统未必都正确设置 `Content-Type`，一律拒绝会让可用性无谓变差；
    而按扩展名推断是**保守**的 —— 扩展名不认识就仍按原类型拒绝。
    """
    normalized = (raw or "").split(";")[0].strip().lower()
    if normalized and normalized != "application/octet-stream":
        return normalized

    suffix = PurePosixPath(file_name or "").suffix.lower()
    return _EXTENSION_CONTENT_TYPES.get(suffix, normalized or "application/octet-stream")


def _safe_file_name(raw: str, *, fallback: str) -> str:
    """把外部提供的文件名安全化。

    ⚠️ 文件名来自**外部系统的 `Content-Disposition`**，是攻击者可影响的输入。
    直接拼进路径可以构造目录穿越。

    处理顺序：

    1. 统一分隔符后只取**最后一段** —— `a/../../etc/passwd` → `passwd`；
    2. 替换文件系统不接受与控制字符；
    3. 去掉首尾的点 —— `..` 与 `.` 会变成空串；
    4. 空则用兜底名；
    5. 超长则保留扩展名截断主体。
    """
    text = str(raw or "").replace("\\", "/")
    name = PurePosixPath(text).name
    name = _UNSAFE_NAME_CHARS.sub("_", name).strip().strip(".")
    if not name:
        return fallback

    if len(name) > _MAX_NAME_LENGTH:
        suffix = PurePosixPath(name).suffix
        keep = max(1, _MAX_NAME_LENGTH - len(suffix))
        name = name[:keep] + suffix

    return name


def _safe_segment(value: str) -> str:
    """把一段文本安全化成单个目录名（用于把审批编号放进路径）。"""
    text = str(value or "").replace("\\", "/")
    segment = _UNSAFE_NAME_CHARS.sub("_", PurePosixPath(text).name).strip().strip(".")
    return segment or "unknown"


def _resolve_under(root: Path, *parts: str) -> Path:
    """在 root 之下解析路径，并保证**不越出 root**。

    这是第二层防护（第一层是 `_safe_file_name` 的清洗）。
    两层都要有：清洗的规则可能被将来的修改放松，
    而"最终路径必须在根目录内"是一个不依赖具体规则的不变量。
    """
    candidate = (root / Path(*parts)).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise PermanentStorageError(
            f"物化路径越出工作目录：{'/'.join(parts)!r}",
            code=ErrorCode.STORAGE_PATH_INVALID,
        ) from exc
    return candidate
