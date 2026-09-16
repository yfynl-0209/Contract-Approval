"""工具 4 与最小查询接口测试（M4 / T10，设计文档 §4.6 修-4 / 修-23）。

本文件守住四类**不会自己报错**的失败：

1. **工具 4 拿不到结果**：接口只回"作业跑得怎么样"，没有任何指向**结果**的东西 ——
   所有验收都通过（作业确实 `succeeded`），只有真的去用才发现拿不到工具 4 该给的东西。
2. **缓存命中却新建作业**：作业总数悄悄增长，而"重复调用是无害的"这句话不再成立。
3. **把 `RESOURCE_NOT_FOUND` 当成 `TASK_NOT_FOUND`**：调用方拿着不存在的 job_id
   去做一次无用的拉取，而真正的问题是 id 写错了。
4. **未下载的附件被入队**：作业会反复失败，而真正的原因（工具 3 没跑过）看不出来。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.auth import Actor, Role
from app.api.deps import get_actor, get_db, get_parser_engine_version
from app.db import Base
from app.enums import ErrorCode, JobStatus, JobType, PageStatus, ParseStatus
from app.errors import AppError
from app.main import app
from app.models import ApprovalAttachment, ApprovalTask, ContractParse, WorkflowJob
from app.ports.parse_document import DocumentPage, StandardDocument
from app.services.parse_service import run_parse
from app.worker import claim_next_job, complete_job


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

CHECKSUM = "a" * 64


# ============================================================
# 夹具
# ============================================================


class _FakeStorage:
    """内存对象存储（T10 只关心"工件写了"，不关心写在哪）。"""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put(self, key: str, data: bytes, *, content_type: str):
        import hashlib

        self.objects[key] = data
        from dataclasses import dataclass

        @dataclass
        class _Ref:
            key: str
            size: int
            sha256: str
            content_type: str

        return _Ref(key, len(data), hashlib.sha256(data).hexdigest(), content_type)

    def get(self, key: str) -> bytes:
        return self.objects[key]

    def exists(self, key: str) -> bool:
        return key in self.objects

    def presign_get(self, key: str, *, expires_in: int) -> str:  # pragma: no cover
        return f"memory://{key}"


class _Clock:
    """给"长任务不阻塞请求"用的小计时器。"""

    def __enter__(self) -> "_Clock":
        import time

        self._start = time.monotonic()
        return self

    def __exit__(self, *exc: object) -> None:
        import time

        self.elapsed = time.monotonic() - self._start


@pytest.fixture()
def env(work_dir: Path):
    """隔离的库 + 覆盖依赖的 TestClient。

    ⚠️ 必须覆盖 `get_db`：不覆盖时接口会打**真实交付库**（`data/app.db`），
    跑一次测试就往里塞一条任务与附件 —— 而测试是"可重复执行"的前提。
    """
    engine = create_engine(f"sqlite:///{(work_dir / 'api.db').as_posix()}", future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, future=True)

    def _override_db():
        session = SessionLocal()
        # 与 `app/db.py::transactional_session` 保持**逐字一致**的语义：
        # AppError 也要提交（业务事实必须先落库再抛出），其余异常回滚。
        # 覆盖成"总是回滚"会让所有业务结论丢失，而测试看起来只是"状态不对"。
        try:
            yield session
        except AppError:
            session.commit()
            raise
        except Exception:
            session.rollback()
            raise
        else:
            session.commit()
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override_db
    # 身份层在本文件里"透明"（理由见 _FULL_ACCESS_ACTOR 的注释）
    app.dependency_overrides[get_actor] = lambda: _FULL_ACCESS_ACTOR
    app.dependency_overrides[get_parser_engine_version] = lambda: "test-engine-1.0"
    client = TestClient(app)
    try:
        yield client, SessionLocal
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _seed(
    SessionLocal: sessionmaker,
    *,
    download_status: str = "success",
    object_key: str | None = f"sha256/aa/aa/{CHECKSUM}.pdf",
    checksum: str | None = CHECKSUM,
) -> tuple[int, int]:
    """造一条任务 + 一份附件，返回 `(task_id, attachment_id)`。"""
    with SessionLocal() as session:
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
            download_status=download_status,
            object_key=object_key,
            file_checksum=checksum,
        )
        session.add(attachment)
        session.commit()
        return task.id, attachment.id


def _ok_document() -> StandardDocument:
    return StandardDocument(
        pages=(
            DocumentPage(
                page=1,
                width=595.0,
                height=842.0,
                bbox_space="pdf-point-top-left",
                rotation=0,
                source="text",
                page_status=PageStatus.OK,
                text="采购合同",
            ),
        )
    )


# ============================================================
# 1. 工具 4：异步入队
# ============================================================


def test_tool4_returns_queryable_task_ref(env) -> None:
    """**验收 13**：工具 4 返回 `job_id`，且请求**立刻**返回（不阻塞解析）。"""
    client, SessionLocal = env
    _task_id, document_id = _seed(SessionLocal)

    with _Clock() as clock:
        response = client.post("/tools/parse_contract_document", json={"document_id": document_id})

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "queued"
    assert body["cache_hit"] is False
    assert clock.elapsed < 1.0, f"长任务不得阻塞请求，实际 {clock.elapsed:.2f}s"

    task_ref = body["task_ref"]
    assert set(task_ref) == {"job_id", "task_id", "status", "status_url"}, (
        "§4.6 的 TaskRef 形状必须逐字一致"
    )
    assert task_ref["status"] == JobStatus.QUEUED.value
    assert task_ref["status_url"] == f"/api/jobs/{task_ref['job_id']}"

    # 落库：占位 pending + 作业 queued
    with SessionLocal() as session:
        parse = session.execute(select(ContractParse)).scalar_one()
        job = session.execute(select(WorkflowJob)).scalar_one()
        assert parse.parse_status == ParseStatus.PENDING.value
        assert parse.parse_version == 1
        assert job.job_type == JobType.PARSE.value
        assert job.job_status == JobStatus.QUEUED.value
        assert job.id == task_ref["job_id"]


def test_tool4_second_call_hits_cache_and_creates_no_job(env) -> None:
    """**验收 48**：缓存命中**不新建作业** —— 作业总数不变、返回原 `job_id`。"""
    client, SessionLocal = env
    _task_id, document_id = _seed(SessionLocal)

    first = client.post("/tools/parse_contract_document", json={"document_id": document_id}).json()
    second = client.post("/tools/parse_contract_document", json={"document_id": document_id}).json()

    assert second["cache_hit"] is True
    assert second["task_ref"]["job_id"] == first["task_ref"]["job_id"]

    with SessionLocal() as session:
        assert session.execute(select(func.count()).select_from(WorkflowJob)).scalar_one() == 1
        assert session.execute(select(func.count()).select_from(ContractParse)).scalar_one() == 1


def test_tool4_reruns_after_a_failed_parse(env) -> None:
    """**验收 30**：失败记录**不构成缓存命中** —— 必须真正重跑。"""
    client, SessionLocal = env
    _task_id, document_id = _seed(SessionLocal)

    first = client.post("/tools/parse_contract_document", json={"document_id": document_id}).json()

    with SessionLocal() as session:
        parse = session.execute(select(ContractParse)).scalar_one()
        parse.parse_status = ParseStatus.FAILED.value
        session.commit()

    second = client.post("/tools/parse_contract_document", json={"document_id": document_id}).json()

    assert second["cache_hit"] is False, "失败记录被当成了缓存命中"
    assert second["result_url"] != first["result_url"], "应当产生新的解析记录"

    with SessionLocal() as session:
        assert session.execute(select(func.count()).select_from(ContractParse)).scalar_one() == 2
        # 新解析记录 → 新幂等键 → 新作业
        assert session.execute(select(func.count()).select_from(WorkflowJob)).scalar_one() == 2


def test_tool4_rejects_undownloaded_attachment(env) -> None:
    """未下载成功的附件**不得入队** —— 入队后只会反复失败，原因看不出来。"""
    client, SessionLocal = env
    _task_id, document_id = _seed(SessionLocal, download_status="pending", object_key=None)

    response = client.post("/tools/parse_contract_document", json={"document_id": document_id})

    assert response.status_code == 200
    assert response.json()["outcome"] == "blocked"
    assert response.json()["error_code"] == ErrorCode.ATTACHMENT_MISSING.value

    with SessionLocal() as session:
        assert session.execute(select(func.count()).select_from(WorkflowJob)).scalar_one() == 0


def test_tool4_rejects_unknown_document_id(env) -> None:
    """`document_id` 不存在 → **404**，而不是 200 的业务结论。

    这两者的区别就是 §`app/api/errors.py` 那张表的前两行：
    "目标不存在"（**调用方给错了 id**）与"业务事实"（附件确实存在、只是还没下载好）
    处置动作完全不同 —— 前者核对 id，后者先去跑工具 3。
    把前者做成 200 blocked，调用方会去重试或跑工具 3，而真正的问题是 id 写错了。
    """
    client, _SessionLocal = env

    response = client.post("/tools/parse_contract_document", json={"document_id": 999999})

    assert response.status_code == 404
    assert response.json()["error_code"] == ErrorCode.RESOURCE_NOT_FOUND.value


def test_tool4_rejects_unknown_payload_keys(env) -> None:
    """键名拼错必须被拒 —— 静默忽略会让调用方以为参数生效了。"""
    client, SessionLocal = env
    _task_id, document_id = _seed(SessionLocal)

    response = client.post(
        "/tools/parse_contract_document", json={"document_id": document_id, "dpi": 300}
    )

    assert response.status_code == 422


# ============================================================
# 2. GET /api/jobs/{job_id}
# ============================================================


def test_job_query_shape_and_empty_result_ref(env) -> None:
    """未成功时 `result_ref` 必须为 `None` —— 否则调用方会去取一个不存在的结果。"""
    client, SessionLocal = env
    _task_id, document_id = _seed(SessionLocal)
    job_id = client.post(
        "/tools/parse_contract_document", json={"document_id": document_id}
    ).json()["task_ref"]["job_id"]

    body = client.get(f"/api/jobs/{job_id}").json()

    assert body["job_id"] == job_id
    assert body["job_type"] == JobType.PARSE.value
    assert body["job_status"] == JobStatus.QUEUED.value
    assert body["attempt_no"] == 0
    assert body["max_attempts"] >= 1
    assert body["status_url"] == f"/api/jobs/{job_id}"
    assert body["result_ref"] is None


def test_unknown_job_is_404_with_resource_not_found(env) -> None:
    """**这是本文件最容易写错的一条**：必须是 `RESOURCE_NOT_FOUND` 而不是
    `TASK_NOT_FOUND` —— 后者会让调用方去跑一次拉取，而问题只是 id 写错了。"""
    client, _SessionLocal = env

    response = client.get("/api/jobs/999999")

    assert response.status_code == 404
    assert response.json()["error_code"] == ErrorCode.RESOURCE_NOT_FOUND.value
    assert response.json()["error_code"] != ErrorCode.TASK_NOT_FOUND.value


def test_unknown_parse_is_404(env) -> None:
    client, _SessionLocal = env

    response = client.get("/api/parses/999999")

    assert response.status_code == 404
    assert response.json()["error_code"] == ErrorCode.RESOURCE_NOT_FOUND.value


# ============================================================
# 3. 端到端：作业成功 → result_ref → 结构化字段
# ============================================================


def _run_the_job(SessionLocal: sessionmaker) -> None:
    """在进程内扮演一次 Worker：领取 → 解析 → 完成（同一事务）。"""
    with SessionLocal() as session:
        claimed = claim_next_job(
            session, job_types=(JobType.PARSE,), worker_id="test-worker", lease_seconds=60
        )
        assert claimed is not None, "应当能领到刚入队的解析作业"

        parse = session.get(ContractParse, claimed.input["parse_id"])
        run_parse(
            session,
            parse=parse,
            data=b"irrelevant",
            storage=_FakeStorage(),
            build_document=lambda _data: _ok_document(),
        )
        complete_job(session, claimed)
        session.commit()


def test_result_ref_appears_after_the_job_succeeds(env) -> None:
    """**验收 28**：作业成功时 `result_ref.parse_id` 非空，
    且 `/api/parses/{parse_id}` 返回结构化字段。"""
    client, SessionLocal = env
    _task_id, document_id = _seed(SessionLocal)
    queued = client.post(
        "/tools/parse_contract_document", json={"document_id": document_id}
    ).json()
    job_id = queued["task_ref"]["job_id"]

    _run_the_job(SessionLocal)

    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["job_status"] == JobStatus.SUCCEEDED.value
    assert job["result_ref"] is not None
    assert job["result_ref"]["result_url"] == queued["result_url"]

    parse = client.get(job["result_ref"]["result_url"]).json()
    assert parse["parse_status"] == ParseStatus.SUCCEEDED.value
    assert parse["parse_error_code"] is None
    assert parse["parser_version"] == "test-engine-1.0+pipe-v1"

    # 工具 4 的契约是"**返回结构化字段**"—— 字段必须内联可读，
    # 不要求调用方去对象存储取工件
    assert parse["basic_info"] is not None
    assert parse["clause_info"] is not None
    assert parse["basic_info"]["schema_version"] == 1
    assert parse["quality"]["ocr_pages"] == 0


def test_failed_gate_still_returns_result_ref_but_no_fields(env) -> None:
    """⚠️ **作业成功 ≠ 解析成功**。

    质量门禁判 `failed` 时作业确实是"跑完了"，`result_ref` 也指得过去 ——
    但 `parse_status` 是 `failed`，**字段为空**。调用方必须看 `parse_status`，
    把 `result_ref` 非空当成"解析成功"会让门禁形同虚设。
    """
    client, SessionLocal = env
    _task_id, document_id = _seed(SessionLocal)
    job_id = client.post(
        "/tools/parse_contract_document", json={"document_id": document_id}
    ).json()["task_ref"]["job_id"]

    with SessionLocal() as session:
        claimed = claim_next_job(
            session, job_types=(JobType.PARSE,), worker_id="w", lease_seconds=60
        )
        assert claimed is not None
        parse = session.get(ContractParse, claimed.input["parse_id"])
        run_parse(
            session,
            parse=parse,
            data=b"x",
            storage=_FakeStorage(),
            # 全部页面为空 → 门禁判 DOCUMENT_EMPTY
            build_document=lambda _d: StandardDocument(
                pages=(
                    DocumentPage(
                        page=1,
                        width=595.0,
                        height=842.0,
                        bbox_space="pdf-point-top-left",
                        rotation=0,
                        source="text",
                        page_status=PageStatus.BLANK,
                        text="",
                    ),
                )
            ),
        )
        complete_job(session, claimed)
        session.commit()

    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["job_status"] == JobStatus.SUCCEEDED.value
    assert job["result_ref"] is not None

    parse = client.get(job["result_ref"]["result_url"]).json()
    assert parse["parse_status"] == ParseStatus.FAILED.value
    assert parse["parse_error_code"] == ErrorCode.DOCUMENT_EMPTY.value
    assert parse["basic_info"] is None, "门禁未通过的记录不得带字段结论"


def test_result_ref_is_none_for_non_parse_jobs(env) -> None:
    """非解析类作业没有 `result_ref` —— 指向一个不存在的资源比 `None` 更糟。"""
    client, SessionLocal = env
    task_id, _document_id = _seed(SessionLocal)

    with SessionLocal() as session:
        from app.workflow.jobs import create_job

        job, _ = create_job(
            session,
            job_type=JobType.PULL,
            task_id=task_id,
            idempotency_key="pull:v1",
            input_payload={"provider": "mock", "tenant_id": "default"},
        )
        # 直接置为成功（这里只关心 result_ref 的判据）
        job.job_status = JobStatus.SUCCEEDED.value
        session.commit()
        job_id = job.id

    assert client.get(f"/api/jobs/{job_id}").json()["result_ref"] is None


# ============================================================
# 4. 解析记录的可追溯字段
# ============================================================


def test_parse_response_exposes_traceability(env) -> None:
    """**验收 10**：`cache_key` / `parser_name` / `parser_version` 均可读。"""
    client, SessionLocal = env
    _task_id, document_id = _seed(SessionLocal)
    client.post("/tools/parse_contract_document", json={"document_id": document_id})
    _run_the_job(SessionLocal)

    with SessionLocal() as session:
        parse_id = session.execute(select(ContractParse.id)).scalar_one()

    body = client.get(f"/api/parses/{parse_id}").json()

    assert body["cache_key"] and len(body["cache_key"]) == 64
    assert body["parser_name"] == "pymupdf"
    assert body["parse_version"] == 1
    # ⚠️ 接口用 `attachment_record_id`（本系统主键），不是 §4.6 字面的 `attachment_id`
    # —— 后者与外部编号同名不同义，沿用会把 §4.5 修-15 要消除的歧义出口给集成方
    assert body["attachment_record_id"] == document_id
    assert "attachment_id" not in body
