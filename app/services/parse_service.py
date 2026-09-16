"""解析服务 —— 缓存占位、工件落盘、质量门禁与任务推进（§3.2 / §3.4 / §4.9 / §4.10）。

## 本模块的四件事

**① 缓存占位闸门**（§3.2 修-9 / 修-22 / 修-31）：
"检查存在"与"创建"合成一个原子动作，且**占位早于 OCR**。
唯一约束只保证"不会有两行"，**不保证"只跑一次 OCR"** ——
两个 Worker 都先 OCR 再插入，约束只能让第二个插入失败，而 OCR 已经跑了两遍，
而 OCR 恰恰是整条链路里最贵的一步。

**② 工件入对象存储**（§3.4 决策③）：页面/文本块/坐标体积大，
数据库只留对象键与摘要。字段结论留在库里，**两边不重复存同一信息**。

**③ 质量门禁**（§4.10）：页状态 → 合同结论 → 任务结论。缺了它，
扫描件 OCR 出一堆乱码也会"解析成功"，然后基于乱码做规则评价，
产出一份**看起来完整**的报告 —— 而**没有任何一条断言会失败**。

**④ 任务推进与多附件聚合**（§4.9）：**"全做完"才放行，而"没有可做的"不是"做完了"**。
把空集合当成"全部完成"是这类聚合最典型的错误 ——
它让"任务只有不支持类型的附件"这种情形产出"审查通过"。

## 事务边界

`reserve_parse()` 与 `run_parse()` 都**不提交** —— 调用方（API / Worker）
负责提交，因为：
- 占位行必须与"创建作业"**同一事务**（§3.2 第 1~2 步）；
- 写字段 + 写工件 + 完成任务必须**同一事务**（§4.4.3）。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.enums import ErrorCode, JobType, LogLevel, LogType, ParseStatus, TaskStatus
from app.errors import AppError, PermanentError
from app.models import ApprovalAttachment, ApprovalTask, ContractParse, ParseArtifact
from app.ports.object_storage import ObjectStorage
from app.ports.parse_document import StandardDocument
from app.services.field_extractor import FieldExtractor
from app.services.log_service import LogService
from app.workflow.job_inputs import ParseJobInput, ParseOptions
from app.workflow.jobs import build_idempotency_key, create_job
from app.workflow.state_machine import can_transition, mark_blocked, transition

#: 解析器标识（进 `parser_name`）。换解析库时改这里 + 适配器。
PARSER_NAME = "pymupdf"
#: 管线版本。**解析逻辑（路由阈值语义、字段模式、规范化）变化时必须 +1** ——
#: 它参与 `cache_key`，因此改了它 = 旧缓存自动失效。
#: 不 +1 的后果：老记录与新记录 `cache_key` 相同，新代码**永远不会被执行**。
PIPELINE_VERSION = "pipe-v1"

#: 工件内容的类型标签
ARTIFACT_STANDARD_DOCUMENT = "standard_document"
ARTIFACT_OCR_PAGES = "ocr_pages"


# ============================================================
# 缓存键与追溯字段
# ============================================================


def parser_version(engine_version: str, *, pipeline_version: str = PIPELINE_VERSION) -> str:
    """`{引擎版本}+{管线版本}`，如 `1.25.1+pipe-v1`。

    ⚠️ **必须区分两个"版本"**：

    | 字段 | 回答的问题 |
    | --- | --- |
    | `parse_version` | **同一附件的第几次解析**（时序、序号） |
    | `parser_version` | **由哪个解析器产生**（可比性、缓存命中） |

    混用会导致一个具体错误：解析器升级后旧记录仍显示"版本 2"，
    于是缓存判定命中同一条，**升级后的解析器永远不会被真正执行**。
    """
    return f"{engine_version}+{pipeline_version}"


def config_digest(options: ParseOptions | None = None) -> str:
    """参与解析的配置摘要（§3.2 要求：DPI、阈值、白名单、规范化版本…）。

    摘要的**构成**必须涵盖"会改变结果的一切"，否则调了参数却看不出差别：
    `cache_key` 是用它算的，漏一项就等于"那个参数改了不生效，且不报错"。
    """
    payload = {
        "options": (options or ParseOptions()).model_dump(mode="json"),
        "pipeline": PIPELINE_VERSION,
        "parser": PARSER_NAME,
    }
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def build_cache_key(*, source_checksum: str, parser_name: str, version: str, digest: str) -> str:
    """`cache_key` = 上述三者与 `source_checksum` 的合成键（定长，便于建索引）。"""
    material = "|".join([parser_name, version, digest, source_checksum])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


# ============================================================
# 缓存占位
# ============================================================


def reserve_parse(
    session: Session,
    *,
    task_id: int,
    attachment_id: int,
    source_checksum: str,
    options: ParseOptions | None = None,
    engine_version: str = "",
    max_attempts: int = 5,
) -> tuple[ContractParse, bool]:
    """**原子地**创建或复用一条 `contract_parses` 占位行。

    Returns:
        `(记录, 是否本次创建)`。`created=False` 表示命中了缓存占位
        —— 调用方**不应重复跑 OCR**。

    实现要点（顺序即正确性）：

    1. 先查"进行中/已成功"的记录（快路径）；
    2. 没有就插入 `pending`，**在 SAVEPOINT 里** —— 并发冲突时只回滚这一步；
    3. 撞 `IntegrityError` 后**再查一次**：插不进去有两种原因，
       必须区分：
       - **缓存占位**冲突（别人抢先）→ 复用他的；
       - **`parse_version` 重复**（并发分配同一个序号）→ 换个序号**重试**。

    ⚠️ 为什么不用"先 `SELECT MAX(parse_version) + 1` 再插入"：那是"先查后写"，
    并发下两个请求会拿到同一个序号，第二个插入失败 —— 而**失败的方式是
    `IntegrityError`**，看起来像缺陷而不像并发。这里保留 SELECT MAX 取候选值，
    但用**唯一冲突重试**兜住并发。§3.2 修-9 禁止的是没有重试的那种写法。

    Raises:
        AppError: 连续多次都无法分配序号（并发强度异常）。
    """
    key = build_cache_key(
        source_checksum=source_checksum,
        parser_name=PARSER_NAME,
        version=parser_version(engine_version),
        digest=config_digest(options),
    )

    for _ in range(max_attempts):
        existing = _find_live_parse(session, attachment_id, key)
        if existing is not None:
            return existing, False

        candidate = ContractParse(
            task_id=task_id,
            attachment_id=attachment_id,
            parse_status=ParseStatus.PENDING.value,
            parser_name=PARSER_NAME,
            parser_version=parser_version(engine_version),
            config_digest=config_digest(options),
            cache_key=key,
            source_checksum=source_checksum,
            parse_version=_next_parse_version(session, attachment_id),
        )
        try:
            # 用 SAVEPOINT 隔开：并发冲突时只回滚这次插入，
            # 不会把调用方在同一事务里的其他改动（例如创建作业）一起丢掉。
            with session.begin_nested():
                session.add(candidate)
                session.flush()
        except IntegrityError:
            session.expire_all()
            continue

        return candidate, True

    raise AppError(
        f"附件 {attachment_id} 的解析占位连续 {max_attempts} 次冲突，放弃",
        code=ErrorCode.INVALID_STATE_TRANSITION,
    )


def _find_live_parse(session: Session, attachment_id: int, key: str) -> ContractParse | None:
    """按**部分唯一索引的同款条件**查既有占位。

    ⚠️ 条件必须与索引逐字一致（`app.models._CACHE_GATE_PREDICATE`）：
    条件宽了会把失败的记录也当成缓存命中（于是**永远重跑不起来**），
    窄了会漏掉真正的占位（于是**并发跑两遍 OCR**）。
    """
    statement = (
        select(ContractParse)
        .where(
            ContractParse.attachment_id == attachment_id,
            ContractParse.cache_key == key,
            ContractParse.parse_status.in_(sorted(ParseStatus.cache_gate_values())),
        )
        .order_by(ContractParse.id.desc())
        .limit(1)
    )
    return session.execute(statement).scalar_one_or_none()


def _next_parse_version(session: Session, attachment_id: int) -> int:
    """下一个解析序号（同一附件内递增）。

    历史记录（含失败）**都要占号**：`UNIQUE(attachment_id, parse_version)` 是全局的，
    跳过号段反而会撞上已被失败记录占用的值。
    """
    statement = select(func.max(ContractParse.parse_version)).where(
        ContractParse.attachment_id == attachment_id
    )
    return (session.execute(statement).scalar() or 0) + 1


# ============================================================
# 质量门禁（§4.10）：页 → 合同
# ============================================================


@dataclass(frozen=True)
class ContractVerdict:
    """合同级结论。`status` 取 `ParseStatus`；`error_code` 仅失败时给。"""

    status: ParseStatus
    error_code: ErrorCode | None = None
    reason: str = ""


def contract_verdict(document: StandardDocument) -> ContractVerdict:
    """把**页状态**汇总成**合同结论**（§4.10）。

    | 情形 | 结论 |
    | --- | --- |
    | **全部**页面可靠识别后均为空 | `failed` + `DOCUMENT_EMPTY` |
    | **任一**页面 `failed` | `failed` + 该页的错误码 |
    | 出现 `uncertain` 页（**默认即视为无法识别**） | `blocked` + `OCR_UNRECOGNIZABLE` |
    | 全部 `ok`（**允许个别 `blank`**） | `succeeded` |

    **三处最容易写错的地方**：

    1. **单个 `blank` 页不是失败**（合同有空白背页很正常）。判据必须是**全称**：
       "**全部**页面为空"才构成 `DOCUMENT_EMPTY`。写成"存在 `blank` 即失败"
       会把正常合同判死；
    2. **`uncertain` 默认判 `blocked`**，而不是按比例容忍。需求写得很直接：
       **图片无法识别 → `blocked`**。容忍比例意味着"识别不了也可以继续出报告"——
       那不是技术细节，是把需求的结论改了；
    3. **门禁是进入 `reviewing` 的前提**：§4.9 说的"解析成功"必须是
       `parse_status == 'succeeded'`（**已过门禁**），而不是"作业跑完了"。
       否则门禁形同虚设。
    """
    pages = document.pages

    failed = [page for page in pages if page.page_status.value == "failed"]
    if failed:
        first = failed[0]
        code = _as_error_code(first.error_code) or ErrorCode.PDF_CORRUPT
        return ContractVerdict(
            ParseStatus.FAILED,
            code,
            f"第 {[p.page for p in failed]} 页解析失败",
        )

    uncertain = [page for page in pages if page.page_status.value == "uncertain"]
    if uncertain:
        return ContractVerdict(
            ParseStatus.BLOCKED,
            ErrorCode.OCR_UNRECOGNIZABLE,
            f"第 {[p.page for p in uncertain]} 页无法可靠识别",
        )

    # ⚠️ 判据是**全称**：全部页面都"可靠识别后为空"才是空文档。
    # 写成"存在 blank 即失败"会把带空白背页的正常合同判死。
    if pages and all(page.page_status.value == "blank" for page in pages):
        return ContractVerdict(
            ParseStatus.FAILED, ErrorCode.DOCUMENT_EMPTY, "全部页面均无文字内容"
        )

    return ContractVerdict(ParseStatus.SUCCEEDED, None, "")


def _as_error_code(value: str | None) -> ErrorCode | None:
    if not value:
        return None
    try:
        return ErrorCode(value)
    except ValueError:
        # 页上的错误码来自枚举，理论上总能解析；解析不了说明它被写坏了，
        # 此时**不猜**，交给调用方退回一个确定性码。
        return None


# ============================================================
# 工具 4 的入口：占位 + 建作业（同一事务）
# ============================================================


@dataclass(frozen=True)
class ParseRequestResult:
    """工具 4 的返回：可查询的作业引用（§4.6 的 `TaskRef`）。"""

    parse_id: int
    job_id: int
    task_id: int | None
    job_status: str
    #: `True` 表示命中了既有占位（缓存），调用方**不该重跑 OCR**
    cache_hit: bool


def request_parse(
    session: Session,
    *,
    document_id: int,
    options: ParseOptions | None = None,
    engine_version: str = "",
) -> ParseRequestResult:
    """工具 4：**只入队，不同步解析**，返回可查询的作业引用。**不提交**。

    顺序即正确性（§3.2 的 5 步）：

    1. 事务内创建或获取 `contract_parses` 占位（`pending`）；
    2. **同一事务内**创建或复用唯一解析作业，输入携带已预留的 `parse_id`；
    3. 只有成功创建占位行的那个请求才算"我认领了这次解析"。

    ⚠️ **占位行必须早于 OCR**。把它挪到 OCR 之后，唯一约束就从
    "防重复执行"退化成"防重复记录"了 —— 两个 Worker 都会先跑完 OCR
    再插入，约束只能让第二个插入失败，而 OCR 已经跑了两遍。
    OCR 是整条链路里最贵的一步，"省一次模型推理"正是缓存存在的全部理由。

    ⚠️ 缓存命中时**不新建作业**（验收 48）：`create_job` 用的是
    `parse:{parse_id}:v1` 这个确定性键，命中即复用同一条作业，
    作业总数不变；本次调用只写一条 `task_logs` 留痕。

    Raises:
        PermanentError: `RESOURCE_NOT_FOUND`（附件记录不存在）、
            `ATTACHMENT_MISSING`（附件未成功下载，或没有校验和）。
    """
    attachment = session.get(ApprovalAttachment, document_id)
    if attachment is None:
        raise PermanentError(
            f"附件记录 {document_id} 不存在", code=ErrorCode.RESOURCE_NOT_FOUND
        )

    # 没下载成功的附件**无法解析**：报具体原因，而不是入队后让 Worker 去发现。
    # 入队再失败的表现是"作业一直失败"，而真正的原因（工具 3 没跑过）看不出来。
    if attachment.download_status != "success" or not attachment.object_key:
        raise PermanentError(
            f"附件 {document_id} 未成功下载（download_status="
            f"{attachment.download_status!r}），请先调用工具 3",
            code=ErrorCode.ATTACHMENT_MISSING,
        )
    if not attachment.file_checksum:
        raise PermanentError(
            f"附件 {document_id} 缺少校验和，无法作为缓存键的来源",
            code=ErrorCode.ATTACHMENT_MISSING,
        )

    parse, created = reserve_parse(
        session,
        task_id=attachment.task_id,
        attachment_id=attachment.id,
        source_checksum=attachment.file_checksum,
        options=options,
        engine_version=engine_version,
    )

    job, _job_created = create_job(
        session,
        job_type=JobType.PARSE,
        task_id=attachment.task_id,
        idempotency_key=build_idempotency_key(JobType.PARSE, str(parse.id), "v1"),
        input_payload={
            "parse_id": parse.id,
            "attachment_record_id": attachment.id,
            "source_checksum": attachment.file_checksum,
            "object_key": attachment.object_key,
            "content_type": _content_type_of(attachment),
            "parse_options": (options or ParseOptions()).model_dump(mode="json"),
        },
    )

    if not created:
        # 缓存命中（§3.2 步 4）：不入队、不跑 OCR，但要**留痕** ——
        # 不留痕的话，"这次调用发生过" 在库里没有任何记录，
        # 而调用方明明拿到了一个 200 与一个 job_id。
        LogService(session).log(
            log_type=LogType.PARSE,
            task_id=attachment.task_id,
            level=LogLevel.INFO,
            message=(
                f"命中解析缓存：附件 {attachment.id} 复用解析记录 {parse.id}"
                f"（parse_version={parse.parse_version}，status={parse.parse_status}）"
            ),
            payload={"job_id": job.id, "cache_hit": True},
        )

    return ParseRequestResult(
        parse_id=parse.id,
        job_id=job.id,
        task_id=attachment.task_id,
        job_status=job.job_status,
        cache_hit=not created,
    )


# ============================================================
# 执行解析
# ============================================================


def run_parse(
    session: Session,
    *,
    parse: ContractParse,
    data: bytes,
    storage: ObjectStorage,
    build_document: Callable[[bytes], StandardDocument],
    options: ParseOptions | None = None,
) -> ContractParse:
    """把 `data` 解析成字段与工件，并推进**这一条**解析记录。**不提交。**

    `build_document` 由调用方注入（"字节 → 标准文档"），
    这样本服务**不 import 任何适配器**：组合根负责把
    `PyMuPdfExtractor` + `OCRGateway` + 阈值组装成那个可调用对象。
    服务层直接 import 适配器会破坏依赖方向，也会让测试必须起真实的 PDF 栈。

    Args:
        parse: 已占位的解析记录（`reserve_parse` 产出）。
        data: 附件字节。
        storage: 对象存储端口。
        build_document: `bytes -> StandardDocument`。

    Returns:
        同一个 `parse` 对象（已就地更新）。
    """
    resolved_options = options or ParseOptions()

    try:
        document = build_document(data)
    except AppError as exc:
        # 确定性/瞬时由错误码决定 —— 这里只如实记录，不替调度器分类。
        parse.parse_status = ParseStatus.FAILED.value
        parse.parse_error_code = exc.code.value
        parse.parse_error = exc.message
        return parse

    verdict = contract_verdict(document)

    # 工件总是写：门禁判失败时，**证据恰恰是人最需要看的** ——
    # 不写的话，人只知道"这份解析不合格"，看不到它到底读到了什么。
    _store_artifact(
        session, parse, storage, document, kind=ARTIFACT_STANDARD_DOCUMENT
    )
    _store_artifact(session, parse, storage, document, kind=ARTIFACT_OCR_PAGES)

    parse.parse_status = verdict.status.value
    parse.parse_error_code = verdict.error_code.value if verdict.error_code else None
    parse.parse_error = verdict.reason or None
    parse.text_coverage = _text_coverage(document)
    parse.ocr_pages = sum(1 for page in document.pages if page.source == "ocr")

    if verdict.status is ParseStatus.SUCCEEDED:
        result = FieldExtractor(document).extract()
        parse.basic_info_json = result.basic_info.model_dump_json()
        parse.clause_info_json = result.clause_info.model_dump_json()

    return parse


def _store_artifact(
    session: Session,
    parse: ContractParse,
    storage: ObjectStorage,
    document: StandardDocument,
    *,
    kind: str,
) -> None:
    """把工件写入对象存储，并在库里留对象键与摘要（§3.4 决策③）。

    键是**内容寻址**的（`sha256/{ab}/{cd}/{sha}.json`），与 M3 附件同一套：
    内容相同就自然复用同一个对象，不需要"先查有没有"。
    """
    payload = json.dumps(
        document.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    key = f"sha256/{digest[:2]}/{digest[2:4]}/{digest}.{kind}.json"

    ref = storage.put(key, payload, content_type="application/json")

    exists = session.execute(
        select(ParseArtifact).where(
            ParseArtifact.parse_id == parse.id,
            ParseArtifact.kind == kind,
            ParseArtifact.artifact_version == 1,
        )
    ).scalar_one_or_none()
    if exists is not None:
        # 重跑同一份附件（历史失败后重解析）：工件内容相同则幂等复用，
        # 不新增行 —— `UNIQUE(parse_id, kind, artifact_version)` 是硬约束。
        exists.object_key = ref.key
        exists.sha256 = ref.sha256
        exists.size_bytes = ref.size
        return

    session.add(
        ParseArtifact(
            parse_id=parse.id,
            kind=kind,
            object_key=ref.key,
            sha256=ref.sha256,
            size_bytes=ref.size,
            content_type="application/json",
            artifact_version=1,
        )
    )
    session.flush()


def _text_coverage(document: StandardDocument) -> float | None:
    """文本层的页面覆盖率（有文本层的页 / 总页数）。

    ⚠️ 这是**文档级**指标，与 `ParseOptions.min_coverage` 的**页级面积比**
    不是一回事。同名不同义最容易在报表里被当成同一个数，因此这里换了个名字
    （`text_coverage` 存的是"有多少页有文本层"）。
    """
    if not document.pages:
        return None
    with_text = sum(1 for page in document.pages if page.source == "text")
    return round(with_text / len(document.pages), 4)


# ============================================================
# 任务推进与多附件聚合（§4.9）
# ============================================================


def advance_task_after_parse(
    session: Session,
    task: ApprovalTask,
    *,
    allowed_types: tuple[str, ...],
) -> TaskStatus:
    """按多附件聚合规则推进任务状态。**不提交。**

    | 情形 | 任务状态 |
    | --- | --- |
    | 目标附件缺失 / 失败 | `blocked`（`ATTACHMENT_MISSING`） |
    | **没有任何可解析的附件** | `blocked`（`ATTACHMENT_TYPE_NOT_ALLOWED`） |
    | 全部目标附件解析成功 | `reviewing` |
    | 其余（还有在跑 / 没跑） | 保持 `parsing` |

    **最后两行的区别是关键**：**"全做完"才放行，而"没有可做的"不是"做完了"**。
    把空集合当成"全部完成"是这类聚合最典型的错误 ——
    它让"任务只有不支持类型的附件"这种情形产出**"审查通过"**。

    ⚠️ "解析成功"必须是 `parse_status == 'succeeded'`（**已过质量门禁**），
    而不是"作业跑完了"。否则门禁形同虚设：作业 `succeeded` 但质量不合格，
    任务照样进入 `reviewing`。
    """
    attachments = (
        session.execute(
            select(ApprovalAttachment).where(ApprovalAttachment.task_id == task.id)
        )
        .scalars()
        .all()
    )
    # ⚠️ 与上方"空元组表示**不在此复核**"（`execute_parse_job` 的门禁，
    # 647 行 `if allowed_types and …`）保持**同一语义**：空 = 用下载时的
    # 权威校验，这里不再复核。曾经这里把空集当成"没有任何允许的类型"，
    # 于是每次解析成功后任务都被判 `blocked/ATTACHMENT_TYPE_NOT_ALLOWED` ——
    # 解析行是绿的，任务却是死的，而单看任何一行都"正常"。
    # （M8 验收走查第 6 条抓到：回写送达后任务无法进入 done，因为它早已 blocked。）
    if allowed_types:
        allowed = {item.lower() for item in allowed_types}
        targets = [
            attachment
            for attachment in attachments
            if _content_type_of(attachment).lower() in allowed
        ]
    else:
        targets = list(attachments)

    if not targets:
        # **空集合不是完成**：任务只有不支持的附件时，不能产出"审查通过"。
        mark_blocked(
            task,
            stage=JobType.PARSE,
            error_code=ErrorCode.ATTACHMENT_TYPE_NOT_ALLOWED,
            message="没有任何可解析的合同附件",
        )
        return TaskStatus(task.task_status)

    broken = [
        attachment
        for attachment in targets
        if attachment.download_status != "success" or not attachment.object_key
    ]
    if broken:
        mark_blocked(
            task,
            stage=JobType.PARSE,
            error_code=ErrorCode.ATTACHMENT_MISSING,
            message=f"附件 {[a.attachment_id for a in broken]} 未成功下载或已缺失",
        )
        return TaskStatus(task.task_status)

    succeeded = {row[0] for row in session.execute(
        select(ContractParse.attachment_id).where(
            ContractParse.task_id == task.id,
            ContractParse.parse_status == ParseStatus.SUCCEEDED.value,
        )
    )}
    # ⚠️ 终局**失败**的解析不是"还在跑"：FAILED / BLOCKED 是终态，
    # 把它们算进 pending 会让门禁失败的任务永远显示"正在解析"。
    gate_failures = (
        session.execute(
            select(ContractParse)
            .where(
                ContractParse.task_id == task.id,
                ContractParse.attachment_id.in_([a.id for a in targets]),
                ContractParse.parse_status.in_(
                    [ParseStatus.FAILED.value, ParseStatus.BLOCKED.value]
                ),
            )
            .order_by(ContractParse.id)
        )
        .scalars()
        .all()
    )
    failed_attachment_ids = {row.attachment_id for row in gate_failures}
    pending_targets = [
        a
        for a in targets
        if a.id not in succeeded and a.id not in failed_attachment_ids
    ]

    if pending_targets:
        # 还有未终局的目标：保持现状（"没有可做的"不是"做完了"）
        return TaskStatus(task.task_status)

    # ⚠️ 全部目标都到了终局，**不等于全部成功**：门禁失败必须让任务 blocked ——
    # 否则用户面对的是"永远正在解析"。这个分支曾经缺失：旧的空集缺陷
    # 恰好把任务判成 blocked（错误码还是错的），把缺口掩盖到了 M8 走查。
    if gate_failures:
        first = gate_failures[0]
        mark_blocked(
            task,
            stage=JobType.PARSE,
            error_code=first.parse_error_code or ErrorCode.UNEXPECTED_ERROR,
            message=f"解析 {first.id} 未通过质量门禁：{first.parse_error}",
        )
        return TaskStatus.BLOCKED

    if task.task_status != TaskStatus.REVIEWING.value:
        transition(task, TaskStatus.REVIEWING, reason="全部目标附件解析成功")
    return TaskStatus.REVIEWING


def execute_parse_job(
    session: Session,
    *,
    job_input: ParseJobInput,
    storage: ObjectStorage,
    document_factory: Callable[[bytes], StandardDocument],
    allowed_types: tuple[str, ...] = (),
) -> ContractParse:
    """`JobType.PARSE` 作业的处理器主体：**填充入队时预留的那一行**。**不提交**。

    ⚠️ **三个必须**：

    1. **复用入队预留的 `parse_id`**（`job_input.parse_id`）—— 另建一行的话，
       `result_ref` 指向的记录永远停在 `pending`，而作业显示成功；
    2. **使用入队时冻结的 `ParseJobInput`**（含解析参数）——
       入队后改配置再执行，等于"作业的声明"与"实际执行"不一致；
    3. **门禁失败必须推进任务状态**：`DOCUMENT_EMPTY` / `OCR_UNRECOGNIZABLE`
       让任务停在 `parsing`，用户看到的是"正在解析"—— 永远。

    Args:
        allowed_types: 允许的附件类型。空元组表示**不在此复核** ——
            权威校验发生在下载时（M3），这里是防御性复核。
    """
    parse = session.get(ContractParse, job_input.parse_id)
    if parse is None:
        raise PermanentError(
            f"解析记录 {job_input.parse_id} 不存在", code=ErrorCode.RESOURCE_NOT_FOUND
        )
    task = session.get(ApprovalTask, parse.task_id)

    # ⚠️ 解析作业开始执行 = 任务正在解析：把 `pending` 推进到 `parsing`。
    # 曾经没有任何代码做这一步，聚合成功后 `parsing → reviewing` 之前的
    # 每条转换都合法，唯独整条链路的第一步缺席 —— 单附件任务在解析完成时
    # 还停在 `pending`，聚合的 `pending → reviewing` 被状态机正确地拒绝，
    # 作业以 INVALID_STATE_TRANSITION 失败。M8 演示现场用真链路拉起时暴露。
    if task is not None and can_transition(task.task_status, TaskStatus.PARSING):
        transition(task, TaskStatus.PARSING, reason="解析作业开始执行")

    content_type = job_input.content_type.split(";")[0].strip().lower()
    if allowed_types and content_type not in allowed_types:
        parse.parse_status = ParseStatus.FAILED.value
        parse.parse_error_code = ErrorCode.ATTACHMENT_TYPE_NOT_ALLOWED.value
        parse.parse_error = f"附件类型 {content_type} 不在允许范围内"
        _block_task(task, parse)
        return parse

    data = storage.get(job_input.object_key)
    # ⚠️ 校验和必须与入队时冻结的一致。不一致说明附件内容在入队后被替换 ——
    # 这是**确定性失败**（重试无意义），且绝不能用当前内容继续解析：
    # 否则 `contract_parses.source_checksum` 记的是 A、实际解析的却是 B，
    # "这条结论依据的是哪份内容"从此无法回答。
    digest = hashlib.sha256(data).hexdigest()
    if digest != job_input.source_checksum:
        raise PermanentError(
            f"附件内容与入队时不一致：入队 checksum={job_input.source_checksum[:16]}…，"
            f"当前={digest[:16]}…。拒绝解析被替换过的内容",
            code=ErrorCode.ATTACHMENT_MISSING,
        )

    run_parse(
        session,
        parse=parse,
        data=data,
        storage=storage,
        build_document=document_factory,
        options=job_input.parse_options,
    )

    # 执行留痕：`log_type=parse`，关联 ID 来自上下文（Worker 侧由
    # `workflow_jobs.correlation_id` 重绑，请求侧由中间件绑定）——
    # 不留这条日志的话，"这次解析发生过"在 `task_logs` 里没有证据，
    # 跨段排障只能靠猜。⚠️ payload 只放标识与状态，不放 object_key / 正文。
    LogService(session).log(
        log_type=LogType.PARSE,
        task_id=parse.task_id,
        level=LogLevel.INFO
        if parse.parse_status == ParseStatus.SUCCEEDED.value
        else LogLevel.WARNING,
        message=f"解析记录 {parse.id} 执行完成（{parse.parse_status}）",
        payload={
            "parse_id": parse.id,
            "parse_status": parse.parse_status,
            "parse_error_code": parse.parse_error_code,
        },
    )

    # ⚠️ 任务状态交给**多附件聚合**统一推进（`advance_task_after_parse`）：
    # 单附件任务在这里只有两种终局——全部成功 → `reviewing`；门禁失败 → `blocked`。
    # 多附件任务在还有未终局的附件时**保持 `parsing`**（部分完成不是终态）。
    # 直接在这里 `mark_blocked` 会把"另一份附件还在跑"的任务错误地判死。
    if task is not None:
        advance_task_after_parse(session, task, allowed_types=allowed_types)

    return parse


def _content_type_of(attachment: ApprovalAttachment) -> str:
    """附件的类型：优先 `content_type`，回落到 `file_type`。

    两个字段都可能为空（不同来源填的不一样），因此**两个都看** ——
    只看一个会让"另一个字段有值"的附件被判成不支持类型，
    于是任务被 `blocked`，而原因（字段填在哪一列）完全看不出来。
    """
    return (attachment.content_type or attachment.file_type or "").strip()
