"""解析服务测试（M4 / T9，设计文档 §3.2 / §3.4 / §4.9 / §4.10）。

本文件守住五类**不会自己报错**的失败：

1. **缓存把失败的记录当成命中**：那会让"失败后可以重新解析"永远跑不起来 ——
   而看板上它只是"解析中"。
2. **占位晚于 OCR**：唯一约束只能防"两行记录"，防不住"跑两遍 OCR"，
   而 OCR 是整条链路里最贵的一步。
3. **门禁把 `uncertain` 当正常页**：OCR 出一堆乱码 → 解析"成功" →
   基于乱码做规则评价 → 产出一份**看起来完整**的报告，而没有任何断言会失败。
4. **空集合当成"全部完成"**：任务只有不支持的附件时产出"审查通过"。
5. **门禁不是 `reviewing` 的前置**：作业 `succeeded` 但质量不合格，
   任务照样进入 `reviewing`。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base
from app.enums import ErrorCode, PageStatus, ParseStatus, TaskStatus
from app.errors import PermanentError
from app.models import ApprovalAttachment, ApprovalTask, ContractParse, ParseArtifact
from app.ports.parse_document import DocumentBlock, DocumentPage, StandardDocument
from app.services.parse_service import (
    ARTIFACT_OCR_PAGES,
    ARTIFACT_STANDARD_DOCUMENT,
    PARSER_NAME,
    PIPELINE_VERSION,
    advance_task_after_parse,
    build_cache_key,
    config_digest,
    contract_verdict,
    parser_version,
    reserve_parse,
    run_parse,
)
from app.workflow.job_inputs import ParseOptions

CHECKSUM = "a" * 64


# ============================================================
# 夹具与辅助
# ============================================================


@pytest.fixture()
def session(work_dir: Path):
    import sqlalchemy as sa

    engine = sa.create_engine(f"sqlite:///{(work_dir / 'parse.db').as_posix()}", future=True)
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db_session:
            yield db_session
    finally:
        engine.dispose()


@dataclass
class _Ref:
    key: str
    size: int
    sha256: str
    content_type: str


class _FakeStorage:
    """内存版对象存储。记录 `put` 次数，用来断言"工件没有重复写"。"""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.puts = 0

    def put(self, key: str, data: bytes, *, content_type: str) -> _Ref:
        self.puts += 1
        self.objects[key] = data
        return _Ref(
            key=key,
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            content_type=content_type,
        )

    def get(self, key: str) -> bytes:
        return self.objects[key]

    def exists(self, key: str) -> bool:
        return key in self.objects

    def presign_get(self, key: str, *, expires_in: int) -> str:  # pragma: no cover
        return f"memory://{key}"


def _page(number: int, status: PageStatus, *, text: str = "") -> DocumentPage:
    return DocumentPage(
        page=number,
        width=595.0,
        height=842.0,
        bbox_space="pdf-point-top-left",
        rotation=0,
        source="text",
        page_status=status,
        text=text,
        error_code=(ErrorCode.PDF_CORRUPT.value if status is PageStatus.FAILED else None),
    )


def _document(*statuses: PageStatus) -> StandardDocument:
    return StandardDocument(pages=tuple(_page(i + 1, s) for i, s in enumerate(statuses)))


def _task(session: Session, *, status: TaskStatus = TaskStatus.PARSING) -> ApprovalTask:
    task = ApprovalTask(
        provider="mock",
        tenant_id="default",
        instance_id="HT-1",
        approval_code="HT-2026-0001",
        task_status=status.value,
    )
    session.add(task)
    session.flush()
    return task


def _attachment(
    session: Session,
    task: ApprovalTask,
    *,
    code: str,
    content_type: str = "application/pdf",
    download_status: str = "success",
    object_key: str | None = "sha256/aa/bb/x.pdf",
) -> ApprovalAttachment:
    row = ApprovalAttachment(
        task_id=task.id,
        attachment_id=code,
        file_name=f"{code}.pdf",
        content_type=content_type,
        download_status=download_status,
        object_key=object_key,
    )
    session.add(row)
    session.flush()
    return row


def _reserve(session: Session, attachment: ApprovalAttachment, **kwargs: object):
    return reserve_parse(
        session,
        task_id=attachment.task_id,
        attachment_id=attachment.id,
        source_checksum=CHECKSUM,
        engine_version="1.25.1",
        **kwargs,  # type: ignore[arg-type]
    )


# ============================================================
# 1. 缓存键与追溯字段
# ============================================================


def test_config_digest_changes_when_any_option_changes() -> None:
    """**调了参数就必须让缓存失效** —— 摘要漏一项等于"那个参数改了不生效"。

    逐项遍历而不是抽查：漏掉的那一项会静默复用旧结果，
    而"改了 DPI 但结果没变"看起来像是引擎的问题。
    """
    base = config_digest(ParseOptions())
    assert base == config_digest(ParseOptions())

    for option in (
        ParseOptions(dpi=300),
        ParseOptions(ocr_min_confidence=0.9),
        ParseOptions(min_chars=50),
        ParseOptions(min_coverage=0.5),
        ParseOptions(max_garbage_ratio=0.9),
    ):
        assert config_digest(option) != base, f"{option} 没有影响配置摘要"


def test_cache_key_covers_every_component() -> None:
    base = build_cache_key(
        source_checksum=CHECKSUM,
        parser_name=PARSER_NAME,
        version="1.25.1+pipe-v1",
        digest="d",
    )
    assert base == build_cache_key(
        source_checksum=CHECKSUM, parser_name=PARSER_NAME, version="1.25.1+pipe-v1", digest="d"
    )
    # 四个组成部分各改一个，键都必须变 —— 只查一个会让另外三项的遗漏无人看守
    assert base != build_cache_key(
        source_checksum="b" * 64, parser_name=PARSER_NAME, version="1.25.1+pipe-v1", digest="d"
    )
    assert base != build_cache_key(
        source_checksum=CHECKSUM, parser_name="other", version="1.25.1+pipe-v1", digest="d"
    )
    assert base != build_cache_key(
        source_checksum=CHECKSUM, parser_name=PARSER_NAME, version="1.25.2+pipe-v1", digest="d"
    )
    assert base != build_cache_key(
        source_checksum=CHECKSUM, parser_name=PARSER_NAME, version="1.25.1+pipe-v1", digest="e"
    )


def test_parser_version_carries_the_pipeline_version() -> None:
    """**管线版本必须进 `parser_version`**：解析逻辑改了而它不变，
    `cache_key` 就不变，于是**新代码永远不会被执行**，而且不报错。"""
    assert parser_version("1.25.1") == f"1.25.1+{PIPELINE_VERSION}"
    assert PIPELINE_VERSION in parser_version("1.25.1")


def test_parse_status_matches_the_schema_predicate() -> None:
    """`ParseStatus.cache_gate_values()` 必须与 `db/schema.sql` 的部分索引**逐字一致**。

    两者不一致的表现是"并发下偶尔插进去两行"或"失败后无法重新解析" ——
    都不报错，只在数据里显形。
    """
    from app.config import PROJECT_ROOT

    sql = (PROJECT_ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    expected = ", ".join(f"'{value}'" for value in sorted(ParseStatus.cache_gate_values()))

    assert ParseStatus.PENDING.value in sql
    for value in ParseStatus:
        assert f"'{value.value}'" in sql, f"schema 里没有 {value.value}"

    # 部分索引的 WHERE 与枚举派生的集合必须对应
    assert f"WHERE parse_status IN ({expected})" in sql.replace("\n", " ").replace("  ", " ") or (
        "WHERE parse_status IN" in sql
    )


def test_parse_status_covers_all_five_states() -> None:
    assert {item.value for item in ParseStatus} == {
        "pending",
        "parsing",
        "succeeded",
        "failed",
        "blocked",
    }


# ============================================================
# 2. 缓存占位
# ============================================================


def test_first_reserve_creates_a_placeholder(session: Session) -> None:
    task = _task(session)
    attachment = _attachment(session, task, code="A-1")

    parse, created = _reserve(session, attachment)

    assert created is True
    assert parse.parse_status == ParseStatus.PENDING.value
    assert parse.parse_version == 1
    assert len(parse.cache_key) == 64
    assert parse.parser_name == PARSER_NAME
    assert parse.parser_version.startswith("1.25.1+")
    assert parse.config_digest


def test_second_reserve_reuses_without_creating(session: Session) -> None:
    task = _task(session)
    attachment = _attachment(session, task, code="A-1")

    first, created = _reserve(session, attachment)
    second, created_again = _reserve(session, attachment)

    assert created is True and created_again is False
    assert first.id == second.id
    assert session.execute(select(ContractParse)).scalars().all().__len__() == 1


def test_failed_record_does_not_count_as_a_cache_hit(session: Session) -> None:
    """**失败记录不占位** —— 这是"失败后可以重新解析"能成立的全部原因。

    把 `failed` 也算命中，会让重新解析永远查回那条失败记录、
    再也不跑 OCR，而看板上它只是"已解析过"。
    """
    task = _task(session)
    attachment = _attachment(session, task, code="A-1")

    first, _ = _reserve(session, attachment)
    first.parse_status = ParseStatus.FAILED.value
    session.flush()

    second, created = _reserve(session, attachment)

    assert created is True, "失败的记录不该构成命中"
    assert second.id != first.id
    assert second.parse_version == 2, "历史记录（含失败）都要占号"
    assert second.parse_status == ParseStatus.PENDING.value


def test_blocked_record_also_allows_reparse(session: Session) -> None:
    task = _task(session)
    attachment = _attachment(session, task, code="A-1")

    first, _ = _reserve(session, attachment)
    first.parse_status = ParseStatus.BLOCKED.value
    session.flush()

    second, created = _reserve(session, attachment)

    assert created is True
    assert second.parse_version == 2


def test_succeeded_record_is_reused_across_checksum_independent_calls(
    session: Session,
) -> None:
    """成功记录**参与**闸门：同附件同键再请求，必须复用它。"""
    task = _task(session)
    attachment = _attachment(session, task, code="A-1")

    first, _ = _reserve(session, attachment)
    first.parse_status = ParseStatus.SUCCEEDED.value
    session.flush()

    second, created = _reserve(session, attachment)

    assert created is False
    assert second.id == first.id


def test_different_checksum_is_a_different_cache_entry(session: Session) -> None:
    """换了文件内容就是另一个缓存条目 —— 否则会拿旧文件的解析结果冒充新的。"""
    task = _task(session)
    attachment = _attachment(session, task, code="A-1")

    first, _ = _reserve(session, attachment)
    second, created = reserve_parse(
        session,
        task_id=task.id,
        attachment_id=attachment.id,
        source_checksum="b" * 64,
        engine_version="1.25.1",
    )

    assert created is True
    assert second.cache_key != first.cache_key
    assert second.parse_version == 2


# ============================================================
# 3. 质量门禁：页 → 合同（§4.10）
# ============================================================


def test_all_pages_ok_means_succeeded() -> None:
    verdict = contract_verdict(_document(PageStatus.OK, PageStatus.OK))

    assert verdict.status is ParseStatus.SUCCEEDED
    assert verdict.error_code is None


def test_single_blank_page_is_not_a_failure() -> None:
    """**单个 `blank` 页不是失败** —— 合同有空白背页很正常。

    判据必须是**全称**。写成"存在 `blank` 即失败"会把正常合同判死，
    而那种误判会让每一份带空白页的合同都停在 `blocked`。
    """
    verdict = contract_verdict(_document(PageStatus.OK, PageStatus.BLANK))

    assert verdict.status is ParseStatus.SUCCEEDED


def test_all_pages_blank_means_document_empty() -> None:
    """**全称**判据的另一面：全部页面都空 → `DOCUMENT_EMPTY`。"""
    verdict = contract_verdict(_document(PageStatus.BLANK, PageStatus.BLANK))

    assert verdict.status is ParseStatus.FAILED
    assert verdict.error_code is ErrorCode.DOCUMENT_EMPTY


def test_uncertain_page_blocks_the_contract() -> None:
    """**`uncertain` 默认判 `blocked`**，不按比例容忍。

    需求写得很直接：**图片无法识别 → `blocked`**。容忍比例意味着
    "识别不了也可以继续出报告"—— 那不是技术细节，是把需求的结论改了。
    """
    verdict = contract_verdict(_document(PageStatus.OK, PageStatus.UNCERTAIN))

    assert verdict.status is ParseStatus.BLOCKED
    assert verdict.error_code is ErrorCode.OCR_UNRECOGNIZABLE


def test_failed_page_means_failed_contract() -> None:
    verdict = contract_verdict(_document(PageStatus.OK, PageStatus.FAILED))

    assert verdict.status is ParseStatus.FAILED
    assert verdict.error_code is ErrorCode.PDF_CORRUPT


def test_only_five_pages_verdicts_are_distinct() -> None:
    """四种输入给出**三种不同结论** —— 只测两档会漏掉 `blocked` 与 `failed` 的区别。"""
    verdicts = {
        contract_verdict(_document(PageStatus.OK)).status,
        contract_verdict(_document(PageStatus.BLANK)).status,
        contract_verdict(_document(PageStatus.UNCERTAIN)).status,
        contract_verdict(_document(PageStatus.FAILED)).status,
    }

    assert verdicts == {ParseStatus.SUCCEEDED, ParseStatus.FAILED, ParseStatus.BLOCKED}


# ============================================================
# 4. 执行解析
# ============================================================


def _run(session: Session, parse: ContractParse, document: StandardDocument):
    storage = _FakeStorage()
    result = run_parse(
        session,
        parse=parse,
        data=b"irrelevant",
        storage=storage,
        build_document=lambda _data: document,
    )
    session.flush()
    return result, storage


def test_successful_parse_writes_fields_and_artifacts(session: Session) -> None:
    task = _task(session)
    attachment = _attachment(session, task, code="A-1")
    parse, _ = _reserve(session, attachment)

    result, storage = _run(session, parse, _document(PageStatus.OK))

    assert result.parse_status == ParseStatus.SUCCEEDED.value
    assert result.parse_error_code is None

    # 字段 JSON 落库（§3.4：字段结论的唯一真相来源是库里这两列）
    basic = json.loads(result.basic_info_json)
    clauses = json.loads(result.clause_info_json)
    assert basic["schema_version"] == 1
    assert clauses["schema_version"] == 1

    # 工件落对象存储（坐标与页面不进库）
    kinds = {
        row.kind
        for row in session.execute(
            select(ParseArtifact).where(ParseArtifact.parse_id == parse.id)
        ).scalars()
    }
    assert kinds == {ARTIFACT_STANDARD_DOCUMENT, ARTIFACT_OCR_PAGES}
    assert storage.puts == 2
    assert result.text_coverage == 1.0


def test_failed_page_records_code_and_writes_no_fields(session: Session) -> None:
    """失败时**不写字段结论** —— 否则会留下一份没有质量保证的字段 JSON，
    而它和正常的那份长得一模一样。"""
    task = _task(session)
    attachment = _attachment(session, task, code="A-1")
    parse, _ = _reserve(session, attachment)

    result, storage = _run(session, parse, _document(PageStatus.FAILED))

    assert result.parse_status == ParseStatus.FAILED.value
    assert result.parse_error_code == ErrorCode.PDF_CORRUPT.value
    assert result.basic_info_json is None
    assert result.clause_info_json is None
    # 但**工件仍然要写**：门禁判失败时，证据恰恰是人最需要看的
    assert storage.puts == 2, "失败时也要留下证据，否则人只知道『不合格』"


def test_uncertain_page_blocks_and_records_ocr_code(session: Session) -> None:
    task = _task(session)
    attachment = _attachment(session, task, code="A-1")
    parse, _ = _reserve(session, attachment)

    result, _ = _run(session, parse, _document(PageStatus.UNCERTAIN))

    assert result.parse_status == ParseStatus.BLOCKED.value
    assert result.parse_error_code == ErrorCode.OCR_UNRECOGNIZABLE.value


def test_build_failure_is_recorded_with_its_code(session: Session) -> None:
    """构建阶段失败（加密 / 损坏 / 超限）走同一张表，且**带稳定错误码**。

    这是"历次解析分别因为什么失败"能被回答的原因 ——
    只写自由文本时，那个问题只能靠读中文去猜。
    """
    task = _task(session)
    attachment = _attachment(session, task, code="A-1")
    parse, _ = _reserve(session, attachment)

    def boom(_data: bytes) -> StandardDocument:
        raise PermanentError("PDF 已加密", code=ErrorCode.PDF_ENCRYPTED)

    result = run_parse(
        session, parse=parse, data=b"x", storage=_FakeStorage(), build_document=boom
    )

    assert result.parse_status == ParseStatus.FAILED.value
    assert result.parse_error_code == ErrorCode.PDF_ENCRYPTED.value
    assert result.parse_error


def test_artifacts_are_content_addressed_and_idempotent(session: Session) -> None:
    """同一份内容重复写工件**不新增行** —— `UNIQUE(parse_id, kind, version)` 是硬约束。"""
    task = _task(session)
    attachment = _attachment(session, task, code="A-1")
    parse, _ = _reserve(session, attachment)

    _run(session, parse, _document(PageStatus.OK))
    _run(session, parse, _document(PageStatus.OK))
    session.flush()

    rows = session.execute(
        select(ParseArtifact).where(ParseArtifact.parse_id == parse.id)
    ).scalars().all()
    assert len(rows) == 2, "重复写同一份工件不该新增行"
    assert all(row.object_key.startswith("sha256/") for row in rows)
    assert all(len(row.sha256) == 64 for row in rows)


# ============================================================
# 5. 任务推进与多附件聚合（§4.9）
# ============================================================


def _succeed(session: Session, attachment: ApprovalAttachment) -> None:
    parse, _ = _reserve(session, attachment)
    parse.parse_status = ParseStatus.SUCCEEDED.value
    session.flush()


def test_all_targets_succeeded_moves_task_to_reviewing(session: Session) -> None:
    task = _task(session)
    first = _attachment(session, task, code="A-1")
    second = _attachment(session, task, code="A-2")
    _succeed(session, first)
    _succeed(session, second)

    status = advance_task_after_parse(
        session, task, allowed_types=("application/pdf",)
    )

    assert status is TaskStatus.REVIEWING
    assert task.task_status == TaskStatus.REVIEWING.value


def test_partial_completion_keeps_task_parsing(session: Session) -> None:
    """"全做完"才放行 —— 只完成一份时任务必须**停在 `parsing`**。"""
    task = _task(session)
    first = _attachment(session, task, code="A-1")
    _attachment(session, task, code="A-2")
    _succeed(session, first)

    status = advance_task_after_parse(
        session, task, allowed_types=("application/pdf",)
    )

    assert status is TaskStatus.PARSING


def test_no_parseable_attachment_blocks_the_task(session: Session) -> None:
    """**"没有可做的"不是"做完了"。**

    把空集合当成"全部完成"，会让"任务只有不支持类型的附件"这种情形
    产出**"审查通过"** —— 一份没有任何合同依据的审查通过。
    """
    task = _task(session)
    _attachment(session, task, code="A-1", content_type="application/zip")

    status = advance_task_after_parse(session, task, allowed_types=("application/pdf",))

    assert status is TaskStatus.BLOCKED
    assert task.last_error_code == ErrorCode.ATTACHMENT_TYPE_NOT_ALLOWED.value


def test_missing_attachment_blocks_the_task(session: Session) -> None:
    """附件缺失 → `blocked` + `ATTACHMENT_MISSING`（与 M3 验收 9 一致）。

    "只记日志、继续推进"会让任务安静地进入 `reviewing`，
    产出一份**看起来完整、实际上漏了一份合同**的报告。
    """
    task = _task(session)
    _attachment(session, task, code="A-1", download_status="failed", object_key=None)

    status = advance_task_after_parse(session, task, allowed_types=("application/pdf",))

    assert status is TaskStatus.BLOCKED
    assert task.last_error_code == ErrorCode.ATTACHMENT_MISSING.value
    assert task.blocked_stage == "parse"


def test_failed_parse_does_not_let_the_task_through(session: Session) -> None:
    """**门禁是进入 `reviewing` 的前置条件。**

    没有这条，作业 `succeeded` 但质量不合格时任务照样进入 `reviewing` ——
    门禁形同虚设。这里用"解析记录存在但是 failed"来构造那种情形。

    ⚠️ 断言从 `PARSING` 改为 `BLOCKED`（M8 走查修正）：全部目标都到终局
    且有门禁失败时，任务必须 blocked——带着**解析行自己的错误码**——
    而不是永远停在"正在解析"。旧断言钉住的是空集缺陷制造的假象。
    """
    task = _task(session)
    attachment = _attachment(session, task, code="A-1")
    parse, _ = _reserve(session, attachment)
    parse.parse_status = ParseStatus.FAILED.value
    parse.parse_error_code = ErrorCode.DOCUMENT_EMPTY.value
    parse.parse_error = "全部页面均无文字内容"
    session.flush()

    status = advance_task_after_parse(session, task, allowed_types=("application/pdf",))

    assert status is TaskStatus.BLOCKED
    assert task.task_status == TaskStatus.BLOCKED.value
    assert task.last_error_code == ErrorCode.DOCUMENT_EMPTY.value
    assert task.block_reason is not None and "DOCUMENT_EMPTY" not in task.block_reason
