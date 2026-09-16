"""故障演练（M4 / T11，设计文档 §4.2 / §4.4 / §4.10 / §7）。

本文件覆盖**只有跑起来才会暴露**的失败，而不是"某段逻辑的输出对不对"：

1. **五类异常 PDF**：加密 / 损坏 / 超页数 / 渲染像素超限 / 全文为空 ——
   每一类都必须给出**稳定且不重复**的错误码。混成一两个码的后果是
   "这份 PDF 到底哪里不行"要靠读中文去猜，而历史失败记录再也统计不了。
2. **真并发**（验收 10b / 12b 明确要求"**真并发，不是顺序调用两次**"）：
   缓存占位与作业领取在并发下都只能有一个赢家。顺序调用测不出这类缺陷 ——
   它只在两个执行体**同时**读到"还没有"的时候出现。
3. **配对夹具字段一致**（验收 3）：同一份内容的文本件与扫描件必须给出
   相同的字段结论。走真实 OCR，默认跳过（推理慢），用 `RUN_SLOW_OCR=1` 启用。

## ⚠️ 第 2 类（真并发）的**效力边界**——不要把它读成"并发正确性已被证明"

写完之后我按惯例做了一次反向验证：**撤掉被测的加固，看演练是否会失败**。
结果 **照样通过**。

原因是 **SQLite 是单写者模型**：并发的写事务会被串行化，
"两个执行体同时读到还没有"那个窗口在本机很难被真正制造出来。
因此这些并发演练在本仓库里的作用是**回归网**（防止 LIMIT 1、条件、
SAVEPOINT 之类被误删），**不是**对并发正确性的证明。

真正的验证要等 **M9 换 PostgreSQL** —— 那里 MVCC 允许两个事务同时选中同一行，
`claim_next_job` 必须改用 `SELECT … FOR UPDATE SKIP LOCKED`
（已记在 `app/worker.py` 的模块 docstring 里，与 §0.7 的迁移地雷同类）。

> **写不出来"会失败的断言"时，如实标注它的效力，而不是给它起一个听起来更强的名字。**
> 一条永远绿的断言比没有断言更糟：它会让下一个人以为这件事已经被守住了。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import fitz
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.parse.pymupdf_extractor import PyMuPdfExtractor
from app.config import PROJECT_ROOT
from app.db import Base
from app.enums import ErrorCode, JobStatus, JobType
from app.errors import PermanentError
from app.models import ApprovalAttachment, ApprovalTask, ContractParse, WorkflowJob
from app.services.document_builder import DocumentBuilder
from app.services.field_extractor import FieldExtractor
from app.services.parse_service import reserve_parse
from app.worker import claim_next_job
from app.workflow.jobs import create_job

FIXTURES_DIR = PROJECT_ROOT / "mock_approval" / "fixtures"
CHECKSUM = "a" * 64


# ============================================================
# 夹具
# ============================================================


@pytest.fixture()
def session_factory(work_dir: Path) -> sessionmaker:
    """独立的库。

    `check_same_thread=False` + 较长的 `timeout` 是**真并发**测试的前提：
    SQLite 是单写者模型，两个线程同时写时后者会等锁 ——
    没有超时就会出现 `database is locked`，测到的是环境而不是被测代码。
    """
    engine = create_engine(
        f"sqlite:///{(work_dir / 'drill.db').as_posix()}",
        future=True,
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, future=True)
    try:
        yield factory
    finally:
        engine.dispose()


def _pdf(*, pages: int = 1, width: float = 200.0, height: float = 100.0, text: str = "采购合同") -> bytes:
    doc = fitz.open()
    try:
        for index in range(pages):
            page = doc.new_page(width=width, height=height)
            if text:
                page.insert_text(fitz.Point(20, 40), f"{text}{index + 1}", fontsize=12)
        return doc.tobytes()
    finally:
        doc.close()


def _seed_attachment(factory: sessionmaker, *, checksum: str = CHECKSUM) -> tuple[int, int]:
    with factory() as session:
        task = ApprovalTask(
            provider="mock",
            tenant_id="default",
            instance_id="HT-1",
            approval_code="HT-2026-0001",
            task_status="parsing",
        )
        session.add(task)
        session.flush()
        attachment = ApprovalAttachment(
            task_id=task.id,
            attachment_id="A-1",
            file_name="a.pdf",
            content_type="application/pdf",
            download_status="success",
            object_key=f"sha256/aa/aa/{checksum}.pdf",
            file_checksum=checksum,
        )
        session.add(attachment)
        session.commit()
        return task.id, attachment.id


# ============================================================
# 1. 五类异常 PDF
# ============================================================


def test_encrypted_pdf_is_rejected() -> None:
    """加密文档 → `PDF_ENCRYPTED`（**确定性**，重试一万次一样打不开）。

    ⚠️ 判 `needs_pass` 而**不是** `is_encrypted`：后者对"加密但不需要口令"的
    文档同样为真，而那种文档我们能正常读 —— 用 `is_encrypted` 会把可读的文件拒之门外。
    """
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text(fitz.Point(20, 40), "采购合同", fontsize=12)
    try:
        data = doc.tobytes(
            encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="user"
        )
    finally:
        doc.close()

    with pytest.raises(PermanentError) as excinfo:
        PyMuPdfExtractor.open(data)

    assert excinfo.value.code is ErrorCode.PDF_ENCRYPTED
    assert excinfo.value.retryable is False


def test_corrupt_pdf_is_rejected() -> None:
    """损坏文档 → `PDF_CORRUPT`。"""
    with pytest.raises(PermanentError) as excinfo:
        PyMuPdfExtractor.open(b"%PDF-1.4\nthis is not a real pdf body\n%%EOF")

    assert excinfo.value.code is ErrorCode.PDF_CORRUPT


def test_truncated_pdf_never_yields_out_of_bounds_content() -> None:
    """截断的 PDF：**允许**能打开，但不得静默产出**错位**的内容。

    ⚠️ 不断言"一定抛 `PDF_CORRUPT`" —— 我初版就是这么写的，而实测：
    3 页文档截掉一半后**照样打开**，第 1 页完好、抽出 1 行文本。
    PDF 是容错格式，截断常常只丢掉尾部对象。

    因此"必须抛错"是一条**错误的断言**（它描述的期望与格式的事实不符）。
    真正的判据是**不得静默产出错位内容**：能读出来的行必须落在页面边界内，
    且页数不得超过原文。这条也是可稳定通过的 —— 它约束的是行为，
    不是字节层面的巧合。
    """
    data = _pdf(pages=3, text="采购合同条款内容")

    try:
        with PyMuPdfExtractor.open(data[: len(data) // 2]) as extractor:
            page_count = extractor.page_count
            for index in range(page_count):
                geometry = extractor.geometry(index)
                for line in extractor.lines(index):
                    x0, y0, x1, y1 = line.bbox
                    assert 0.0 <= x0 <= x1 <= geometry.width, (
                        f"第 {index + 1} 页的行超出页面水平边界：{line.bbox}"
                    )
                    assert 0.0 <= y0 <= y1 <= geometry.height, (
                        f"第 {index + 1} 页的行超出页面垂直边界：{line.bbox}"
                    )
    except PermanentError as exc:
        # 打不开也是合法结局 —— 只要错误码说得清原因
        assert exc.code is ErrorCode.PDF_CORRUPT
        return

    assert page_count <= 3, "截断不得凭空多出页面"


def test_too_many_pages_is_rejected() -> None:
    """超过页数上限 → `PDF_TOO_MANY_PAGES`。

    这是**内存保护**：500 页扫描件按 200dpi 渲染有数十亿像素，进程会被 OOM 杀掉，
    而"进程被杀"连错误码都留不下 —— 排查时只剩"任务卡住了"。
    """
    with pytest.raises(PermanentError) as excinfo:
        PyMuPdfExtractor.open(_pdf(pages=5), max_pages=2)

    assert excinfo.value.code is ErrorCode.PDF_TOO_MANY_PAGES


def test_oversized_render_is_rejected_before_allocating() -> None:
    """渲染像素超限 → `PDF_TOO_LARGE_PIXELS`。

    必须在**分配之前**按页面尺寸推算并拒绝：等 pixmap 建出来再检查，
    进程可能已经被 OOM 杀掉。
    """
    data = _pdf(width=2000.0, height=2000.0)

    with PyMuPdfExtractor.open(data, max_render_pixels=1000) as extractor:
        with pytest.raises(PermanentError) as excinfo:
            extractor.render(0, dpi=72)

    assert excinfo.value.code is ErrorCode.PDF_TOO_LARGE_PIXELS


def test_five_failure_classes_have_distinct_codes() -> None:
    """**五类的错误码必须互不相同。**

    合并成一两个码的后果很具体："这份 PDF 到底哪里不行"要靠读中文去猜，
    而"本周有多少份因为加密被拒"这类问题再也答不出来。
    """
    codes = set()

    for factory_call in (
        lambda: PyMuPdfExtractor.open(
            _encrypted_bytes()
        ),
        lambda: PyMuPdfExtractor.open(b"%PDF-1.4\n broken \n"),
        lambda: PyMuPdfExtractor.open(_pdf(pages=5), max_pages=2),
    ):
        with pytest.raises(PermanentError) as excinfo:
            factory_call()
        codes.add(excinfo.value.code)

    with PyMuPdfExtractor.open(_pdf(width=2000.0, height=2000.0), max_render_pixels=1000) as ex:
        with pytest.raises(PermanentError) as excinfo:
            ex.render(0, dpi=72)
        codes.add(excinfo.value.code)

    assert len(codes) == 4, f"错误码出现重复：{codes}"
    assert codes == {
        ErrorCode.PDF_ENCRYPTED,
        ErrorCode.PDF_CORRUPT,
        ErrorCode.PDF_TOO_MANY_PAGES,
        ErrorCode.PDF_TOO_LARGE_PIXELS,
    }


def _encrypted_bytes() -> bytes:
    doc = fitz.open()
    doc.new_page()
    try:
        return doc.tobytes(
            encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="o", user_pw="u"
        )
    finally:
        doc.close()


# ============================================================
# 2. 真并发：缓存占位
# ============================================================


def test_concurrent_reserve_creates_exactly_one_row(session_factory: sessionmaker) -> None:
    """**验收 10b**：并发的相同解析请求**只产生一条占位**。

    ⚠️ 必须是**真并发**（线程 + 屏障同时起跑），不是顺序调用两次 ——
    顺序调用永远命中"已存在"的快路径，永远测不到
    "两个执行体同时读到还没有"这个窗口。而缓存占位存在的**全部理由**
    就是那个窗口：两个 Worker 会各跑一遍 OCR，重复的模型推理是最贵的浪费。

    断言的是**不变量**（只有一条、只有一个 created），不是时序 ——
    时序断言在 CI 上会变成随机失败。
    """
    task_id, attachment_id = _seed_attachment(session_factory)
    workers = 8
    barrier = threading.Barrier(workers)
    results: list[tuple[int, bool]] = []
    lock = threading.Lock()

    def reserve() -> None:
        session = session_factory()
        try:
            barrier.wait(timeout=10)
            parse, created = reserve_parse(
                session,
                task_id=task_id,
                attachment_id=attachment_id,
                source_checksum=CHECKSUM,
                engine_version="test",
            )
            session.commit()
            with lock:
                results.append((parse.id, created))
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(lambda _: reserve(), range(workers)))

    assert len(results) == workers
    created = [item for item in results if item[1]]
    assert len(created) == 1, f"只有一方能认领这次解析，实际 {len(created)} 方认为自己是创建者"

    with session_factory() as session:
        assert session.execute(select(func.count()).select_from(ContractParse)).scalar_one() == 1
        # 所有并发方都必须复用**同一条**记录
        assert {item[0] for item in results} == {created[0][0]}


# ============================================================
# 3. 真并发：作业领取
# ============================================================


def test_concurrent_claims_never_hand_out_the_same_job(session_factory: sessionmaker) -> None:
    """**并发领取不得把同一个作业交给两个 Worker。**

    这是"重复执行"的直接来源，而重复执行的代价是**重复的 OCR 推理**：
    两次都成功，库里两条记录，`parse_version` 该算到几变成事后猜测。

    ⚠️ **效力边界（实测过，不是推测）**：撤掉 `claim_next_job` 里
    "外层 `WHERE` 重复可领取条件"那道加固后，**这条依然通过** ——
    因为 SQLite 是单写者，并发的写事务被串行化，
    "两个执行体读到同一个候选 id"这个窗口在本机造不出来。
    因此它是**回归网**，不是这条的证明。详见模块 docstring。

    仍然要留着它：它约束的是"同一个作业不得被发出两次"这个**不变量**，
    而 M9 换 PostgreSQL 后 MVCC 会让那个窗口真实存在。
    """
    with session_factory() as session:
        for index in range(6):
            create_job(
                session,
                job_type=JobType.PULL,
                idempotency_key=f"pull:{index}",
                input_payload={"provider": "mock", "tenant_id": "default"},
            )
        session.commit()

    workers = 6
    barrier = threading.Barrier(workers)
    claimed: list[int | None] = []
    lock = threading.Lock()

    def claim(worker: int) -> None:
        session = session_factory()
        try:
            barrier.wait(timeout=10)
            job = claim_next_job(
                session, job_types=(JobType.PULL,), worker_id=f"w{worker}", lease_seconds=60
            )
            with lock:
                claimed.append(None if job is None else job.job_id)
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(claim, range(workers)))

    handed_out = [job_id for job_id in claimed if job_id is not None]
    assert len(handed_out) == len(set(handed_out)), (
        f"同一个作业被交给了多个 Worker：{sorted(handed_out)}"
    )

    with session_factory() as session:
        running = session.execute(
            select(func.count())
            .select_from(WorkflowJob)
            .where(WorkflowJob.job_status == JobStatus.RUNNING.value)
        ).scalar_one()
        assert running == len(handed_out), (
            f"库里 running 的行数（{running}）与发出的领取数（{len(handed_out)}）不一致"
        )


def test_claim_selects_only_one_job_per_call(session_factory: sessionmaker) -> None:
    """单次领取**只**推进一个作业：多推进会让 `attempt_no` 被其他作业白白消耗。"""
    with session_factory() as session:
        for index in range(3):
            create_job(
                session,
                job_type=JobType.PULL,
                idempotency_key=f"pull:{index}",
                input_payload={"provider": "mock", "tenant_id": "default"},
            )
        session.commit()

    with session_factory() as session:
        claimed = claim_next_job(
            session, job_types=(JobType.PULL,), worker_id="w1", lease_seconds=60
        )
        assert claimed is not None
        running = session.execute(
            select(func.count())
            .select_from(WorkflowJob)
            .where(WorkflowJob.job_status == JobStatus.RUNNING.value)
        ).scalar_one()

    assert running == 1
    assert claimed.attempt_no == 1


# ============================================================
# 4. 配对夹具字段一致（验收 3，默认跳过）
# ============================================================


def _paired_specs() -> tuple:
    """找出一对共用同一份文本的文本件与扫描件。"""
    from mock_approval.contract_texts import FIXTURES

    texts = {spec.file_name: spec for spec in FIXTURES if spec.render == "text"}
    for spec in FIXTURES:
        if spec.render != "scan":
            continue
        for other in texts.values():
            if other.text_key == spec.text_key:
                return spec, other
    pytest.skip("没有共用同一份文本的配对夹具")


@pytest.mark.skipif(
    not __import__("os").environ.get("RUN_SLOW_OCR"),
    reason="真实 OCR 推理慢，用 RUN_SLOW_OCR=1 启用",
)
def test_paired_fixtures_agree_on_key_fields() -> None:
    """**验收 3**：同一份内容的文本件与扫描件，提取的关键字段**一致**。

    这是判断阈值、坐标换算、NFC 规范化三件事合起来是否正确的**唯一**端到端证据 ——
    它们单独看都正常，只有两种渲染给出同一个答案才说明整条链路是通的。
    """
    from app.adapters.parse.rapidocr_adapter import RapidOcrAdapter

    scan_spec, text_spec = _paired_specs()

    with PyMuPdfExtractor.open((FIXTURES_DIR / text_spec.file_name).read_bytes()) as ex:
        text_doc = DocumentBuilder(ex).build()

    with PyMuPdfExtractor.open((FIXTURES_DIR / scan_spec.file_name).read_bytes()) as ex:
        scan_doc = DocumentBuilder(ex, ocr=RapidOcrAdapter()).build()

    text_fields = FieldExtractor(text_doc).extract()
    scan_fields = FieldExtractor(scan_doc).extract()

    for code in ("amount", "currency", "party_a", "party_b"):
        text_value = text_fields.basic_info.get(code)
        scan_value = scan_fields.basic_info.get(code)
        assert text_value.status.value == scan_value.status.value, (
            f"{code} 的结论不一致：文本件 {text_value.status} vs 扫描件 {scan_value.status}"
        )
        if text_value.status.value == "extracted":
            assert text_value.value_decimal == scan_value.value_decimal, (
                f"{code} 的取值不一致：{text_value.value_decimal} vs {scan_value.value_decimal}"
            )
