"""M6 / Task 0：M4 PARSE 作业接入真实 Worker 的集成测试。

## 这条链路此前缺的是什么

M4 完成时，解析的所有部件都在（`request_parse` 预留占位并建作业、
`parse_service` 的解析流水线、`Worker` 的领取与租约），
但**没有任何组装点**把 `JobType.PARSE` 的作业交给那条流水线 ——
于是"入队 → Worker 领取 → 解析 → 结果"从未整条跑通过。

## 三条必须由本文件钉住的"写错了也不报错"

1. **作业必须执行入队时预留的那个 `parse_id`**：另建一条解析行的话，
   `result_ref` 指向的记录永远是空的，而作业显示成功；
2. **门禁失败必须推进任务状态**：`DOCUMENT_EMPTY` / `OCR_UNRECOGNIZABLE`
   让任务停在 `parsing` 的话，用户看到的是"正在解析"——**永远**；
3. **入队时冻结的 `ParseJobInput` 必须被使用**：入队后改配置再执行，
   用了新配置等于"作业的声明"与"实际执行"不一致。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.config import PROJECT_ROOT
from app.enums import JobStatus, JobType, ParseStatus, TaskStatus
from app.models import ApprovalAttachment, ApprovalTask, ContractParse

SCHEMA_FILE = PROJECT_ROOT / "db" / "schema.sql"


def _checksum_of(data: bytes) -> str:
    """附件内容的真实校验和 —— 夹具必须与它一致，否则 checksum 硬校验会拒收。"""
    import hashlib

    return hashlib.sha256(data).hexdigest()


@pytest.fixture()
def factory(work_dir: Path) -> sessionmaker:
    """独立的临时库（schema.sql 建表）。Worker 需要的是**会话工厂**。"""
    path = work_dir / "parse-worker.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()

    engine = create_engine(f"sqlite:///{path.as_posix()}", future=True)
    try:
        yield sessionmaker(bind=engine, future=True)
    finally:
        engine.dispose()


class _Storage:
    """只实现 `get`/`put`/`exists` 的假对象存储。"""

    def __init__(self, objects: dict[str, bytes]) -> None:
        self._objects = objects

    def put(self, key: str, data: bytes, *, content_type: str):
        from app.ports.object_storage import ObjectRef

        import hashlib

        self._objects[key] = data
        return ObjectRef(
            key=key,
            sha256=hashlib.sha256(data).hexdigest(),
            size=len(data),
            content_type=content_type,
        )

    def get(self, key: str) -> bytes:
        return self._objects[key]

    def exists(self, key: str) -> bool:
        return key in self._objects

    def presign_get(self, key: str, *, expires_in: int) -> str:  # pragma: no cover
        return f"memory://{key}"


def _pdf_bytes(*, pages: int = 1, text: str | None = "采购合同条款") -> bytes:
    import fitz

    doc = fitz.open()
    try:
        for index in range(pages):
            page = doc.new_page(width=200, height=100)
            if text:
                page.insert_text(fitz.Point(20, 40), f"{text}{index + 1}", fontsize=10)
        return doc.tobytes()
    finally:
        doc.close()


def _empty_pdf_bytes() -> bytes:
    return _pdf_bytes(text=None)


def test_worker_executes_the_parse_job_and_reuses_the_reserved_parse_id(
    factory: sessionmaker, work_dir: Path
) -> None:
    """入队 → Worker 领取 → 解析 → **同一个** `parse_id` 变为终态。

    ⚠️ 断言"没有第二条解析行"是本条的灵魂：解析流水线如果新建记录而不
    推进入队时预留的那条，`result_ref` 指向的记录永远停在 `pending`，
    而作业显示 `succeeded` —— 两边各自看都正常。
    """
    objects: dict[str, bytes] = {}
    data = _pdf_bytes()
    objects["attach-1"] = data

    with factory() as session:
        from app.adapters.parse.pymupdf_extractor import ENGINE_VERSION
        from app.services.parse_service import request_parse

        task = ApprovalTask(
            provider="mock",
            tenant_id="default",
            instance_id="HT-1",
            approval_code="HT-2026-0001",
            task_status=TaskStatus.PARSING.value,
        )
        session.add(task)
        session.flush()

        attachment = ApprovalAttachment(
            task_id=task.id,
            attachment_id="ATT-0001",
            file_name="contract.pdf",
            object_key="attach-1",
            file_checksum=_checksum_of(data),
            download_status="success",
            content_type="application/pdf",
        )
        session.add(attachment)
        session.flush()

        result = request_parse(
            session, document_id=attachment.id, engine_version=ENGINE_VERSION
        )
        session.commit()

        assert result.parse_id > 0
        parse_id = result.parse_id

        rows = session.execute(
            select(func.count()).select_from(ContractParse)
        ).scalar_one()
        assert rows == 1, "入队时恰好预留一条解析记录"

    # ---- Worker 用与生产入口相同的 make_handler 领取并执行 ----
    from app.worker import Worker
    from scripts.run_worker import make_handler

    worker = Worker(
        factory,
        make_handler(_Storage(objects)),
        job_types=[JobType.PARSE],
    )
    assert worker.run_once() is True, "应当领到那条 PARSE 作业"

    with factory() as check:
        from app.models import WorkflowJob

        parse = check.get(ContractParse, parse_id)
        job = check.get(WorkflowJob, result.job_id)

        assert parse is not None
        # ⚠️ 先看作业：它失败时会把原因记在 last_error_text ——
        # 处理器抛出的异常被 Worker 捕获并回滚，解析行会退回 pending，
        # 若不先断言作业，根因就被测试自己吞掉了。
        assert job.job_status == JobStatus.SUCCEEDED.value, (
            f"作业失败：{job.last_error_code} / {job.last_error_text}"
        )
        assert parse.parse_status in {
            ParseStatus.SUCCEEDED.value,
            ParseStatus.FAILED.value,
        }, "入队预留的那条解析记录必须被推进到终态"

        rows = check.execute(select(func.count()).select_from(ContractParse)).scalar_one()
        assert rows == 1, "不得另建第二条解析记录"


def test_worker_with_default_allowed_types_does_not_block_the_task(
    factory: sessionmaker, work_dir: Path
) -> None:
    """`make_handler` 默认 `allowed_types=()` = "不复核类型"——任务**不得**被判 blocked。

    曾经聚合侧（`advance_task_after_parse`）把空集当成"没有任何允许的类型"：
    解析行 `succeeded`、任务却 `blocked/ATTACHMENT_TYPE_NOT_ALLOWED` ——
    两行各自看都"正常"，只有走到回写（`done` 不可从 `blocked` 进入）才会暴露。
    M8 验收走查第 6 条抓到的是它的下游症状，这条测试钉住病灶本身。
    """
    objects: dict[str, bytes] = {}
    data = _pdf_bytes()
    objects["attach-1"] = data

    with factory() as session:
        from app.adapters.parse.pymupdf_extractor import ENGINE_VERSION
        from app.services.parse_service import request_parse

        task = ApprovalTask(
            provider="mock",
            tenant_id="default",
            instance_id="HT-1",
            approval_code="HT-2026-0001",
            task_status=TaskStatus.PARSING.value,
        )
        session.add(task)
        session.flush()

        attachment = ApprovalAttachment(
            task_id=task.id,
            attachment_id="ATT-0001",
            file_name="contract.pdf",
            object_key="attach-1",
            file_checksum=_checksum_of(data),
            download_status="success",
            content_type="application/pdf",
        )
        session.add(attachment)
        session.flush()

        result = request_parse(
            session, document_id=attachment.id, engine_version=ENGINE_VERSION
        )
        session.commit()

    # ⚠️ **不传** allowed_types —— 正是生产入口曾经的调用形态（已修）。
    from app.worker import Worker
    from scripts.run_worker import make_handler

    worker = Worker(
        factory, make_handler(_Storage(objects)), job_types=[JobType.PARSE]
    )
    assert worker.run_once() is True

    with factory() as check:
        from app.enums import TaskStatus as TS

        task = check.execute(select(ApprovalTask)).scalars().first()
        assert task is not None
        assert task.task_status == TS.REVIEWING.value, (
            f"解析成功后任务应进入 reviewing，实际 {task.task_status}"
            f"（blocked_stage={task.blocked_stage}，"
            f"last_error_code={task.last_error_code}）"
        )


def test_worker_parse_logs_keep_the_api_correlation_id_and_use_the_parse_log_type(
    factory: sessionmaker, work_dir: Path
) -> None:
    """API 段与 Worker 段必须共用同一个 `correlation_id`；PARSE 执行日志用 `LogType.PARSE`。

    关联 ID 跨进程只能靠 `workflow_jobs.correlation_id` 这座桥：
    请求侧绑定 → 作业行落库 → Worker 领取后**重绑**再执行。
    Worker 不重绑，或解析执行的留痕日志落错类型（比如 `SYSTEM`），
    排障时"这条日志属于哪次请求 / 哪个阶段"就追不下去 —— 两边各自都正常。
    """
    objects: dict[str, bytes] = {}
    data = _pdf_bytes()
    objects["attach-1"] = data

    from app.context import correlation_scope

    with factory() as session:
        from app.adapters.parse.pymupdf_extractor import ENGINE_VERSION
        from app.services.parse_service import request_parse

        task = ApprovalTask(
            provider="mock",
            tenant_id="default",
            instance_id="HT-1",
            approval_code="HT-2026-0004",
            task_status=TaskStatus.PARSING.value,
        )
        session.add(task)
        session.flush()

        attachment = ApprovalAttachment(
            task_id=task.id,
            attachment_id="ATT-0004",
            file_name="contract.pdf",
            object_key="attach-1",
            file_checksum=_checksum_of(data),
            download_status="success",
            content_type="application/pdf",
        )
        session.add(attachment)
        session.flush()

        # ---- API 段：与请求中间件同机制地绑定关联 ID 后入队 ----
        with correlation_scope("req-parse-link-1"):
            result = request_parse(
                session, document_id=attachment.id, engine_version=ENGINE_VERSION
            )
        session.commit()

        from app.models import WorkflowJob

        job = session.get(WorkflowJob, result.job_id)
        assert job is not None
        assert job.correlation_id == "req-parse-link-1", (
            "API 段必须把请求侧的关联 ID 写进作业行 —— 这是它跨进程的唯一载体"
        )

    # ---- Worker 段：进程上下文里**没有任何绑定**（真实 Worker 进程如此）----
    from app.context import get_correlation_id

    assert get_correlation_id() is None, "测试前置：Worker 段开始时上下文里不应有关联 ID"

    from app.worker import Worker
    from scripts.run_worker import make_handler

    worker = Worker(factory, make_handler(_Storage(objects)), job_types=[JobType.PARSE])
    assert worker.run_once() is True

    with factory() as check:
        from app.enums import LogType
        from app.models import TaskLog

        logs = list(
            check.execute(select(TaskLog).where(TaskLog.task_id == result.task_id)).scalars()
        )
        assert logs, "PARSE 执行必须留痕 —— 否则'执行过'这件事在库里没有证据"

        for log in logs:
            assert log.correlation_id == "req-parse-link-1", (
                f"Worker 段日志的关联 ID 必须与 API 段一致，"
                f"实际 {log.correlation_id!r}（log_type={log.log_type!r}）"
            )

        parse_logs = [log for log in logs if log.log_type == LogType.PARSE.value]
        assert parse_logs, (
            f"PARSE 执行的留痕日志必须是 log_type=parse，"
            f"实际类型：{[log.log_type for log in logs]}"
        )


def test_gate_failure_moves_the_task_to_blocked_not_stuck_in_parsing(
    factory: sessionmaker, work_dir: Path
) -> None:
    """`DOCUMENT_EMPTY` 必须让任务进入 `blocked`，不能永久停在 `parsing`。

    用户看到的"正在解析"如果永远不变化，比直接报错更糟 ——
    它让人以为系统还在工作。
    """
    objects: dict[str, bytes] = {}
    data = _empty_pdf_bytes()
    objects["attach-1"] = data

    with factory() as session:
        from app.adapters.parse.pymupdf_extractor import ENGINE_VERSION
        from app.services.parse_service import request_parse

        task = ApprovalTask(
            provider="mock",
            tenant_id="default",
            instance_id="HT-1",
            approval_code="HT-2026-0002",
            task_status=TaskStatus.PARSING.value,
        )
        session.add(task)
        session.flush()

        attachment = ApprovalAttachment(
            task_id=task.id,
            attachment_id="ATT-0002",
            file_name="empty.pdf",
            object_key="attach-1",
            file_checksum=_checksum_of(data),
            download_status="success",
            content_type="application/pdf",
        )
        session.add(attachment)
        session.flush()

        request_parse(session, document_id=attachment.id, engine_version=ENGINE_VERSION)
        session.commit()

    from app.worker import Worker
    from scripts.run_worker import make_handler

    worker = Worker(factory, make_handler(_Storage(objects)), job_types=[JobType.PARSE])
    worker.run_once()

    with factory() as check:
        task = check.execute(select(ApprovalTask)).scalars().first()
        parse = check.execute(select(ContractParse)).scalars().first()

        assert parse.parse_status == ParseStatus.FAILED.value
        assert parse.parse_error_code == "DOCUMENT_EMPTY"
        assert task.task_status == TaskStatus.BLOCKED.value, (
            f"门禁失败后任务必须进入 blocked，实际为 {task.task_status!r} —— "
            "停在 parsing 会让用户以为系统还在工作"
        )


def test_checksum_mismatch_fails_deterministically_and_retry_reuses_the_parse_id(
    factory: sessionmaker, work_dir: Path
) -> None:
    """入队后**替换**附件内容 → 确定性失败；重试仍复用同一 `parse_id`。

    两件事各自致命：

    1. **用当前内容继续解析**：`contract_parses.source_checksum` 记的是 A、
       实际解析的是 B —— "这条结论依据哪份内容"从此无法回答；
    2. **重试时另建解析行**：重试是"把同一件事再试一次"，
       新建记录会让 `result_ref.parse_id` 与实际执行的记录分家。

    ⚠️ 确定性失败 = 重试**不会**变绿（内容没变、结果就不会变）——
    这正是要把校验和放进**冻结输入**里校验的原因。
    """
    objects: dict[str, bytes] = {}
    data = _pdf_bytes()
    objects["attach-1"] = data

    with factory() as session:
        from app.adapters.parse.pymupdf_extractor import ENGINE_VERSION
        from app.services.parse_service import request_parse

        task = ApprovalTask(
            provider="mock",
            tenant_id="default",
            instance_id="HT-1",
            approval_code="HT-2026-0003",
            task_status=TaskStatus.PARSING.value,
        )
        session.add(task)
        session.flush()

        attachment = ApprovalAttachment(
            task_id=task.id,
            attachment_id="ATT-0003",
            file_name="contract.pdf",
            object_key="attach-1",
            file_checksum=_checksum_of(data),
            download_status="success",
            content_type="application/pdf",
        )
        session.add(attachment)
        session.flush()

        result = request_parse(
            session, document_id=attachment.id, engine_version=ENGINE_VERSION
        )
        session.commit()

    # 入队之后、执行之前：**替换**对象存储里的内容（校验和随之改变）
    objects["attach-1"] = _pdf_bytes(text="被替换后的合同正文")

    from app.worker import Worker
    from scripts.run_worker import make_handler

    worker = Worker(factory, make_handler(_Storage(objects)), job_types=[JobType.PARSE])
    assert worker.run_once() is True

    with factory() as check:
        from app.models import WorkflowJob

        job = check.get(WorkflowJob, result.job_id)
        parse = check.get(ContractParse, result.parse_id)
        rows = check.execute(select(func.count()).select_from(ContractParse)).scalar_one()

        assert job.job_status == JobStatus.FAILED.value, (
            f"内容被替换必须确定性失败，实际 {job.job_status}："
            f"{job.last_error_code} / {job.last_error_text}"
        )
        assert "不一致" in (job.last_error_text or "")
        assert parse.parse_status == ParseStatus.PENDING.value, (
            "被替换的内容不得被解析 —— 解析记录保持未处理"
        )
        assert rows == 1, "失败重试前不得另建解析行"

    # 确定性失败**不重试**：`PermanentError` 让作业进入终态 `failed`，
    # 领取器不会再碰它 —— "重试无意义"的正确实现就是不再重试
    # （瞬态失败的租约重试仍会复用同一 parse_id，见 execute_parse_job 只填充预留行）。
    assert worker.run_once() is False, "终态失败不得再次被领取"

    with factory() as check:
        from app.models import WorkflowJob

        job = check.get(WorkflowJob, result.job_id)
        rows = check.execute(select(func.count()).select_from(ContractParse)).scalar_one()

        assert job.job_status == JobStatus.FAILED.value
        assert rows == 1, "即便作业终态失败，也不得另建解析行"
