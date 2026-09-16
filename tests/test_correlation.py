"""关联 ID 贯穿测试（M4 / T5，设计文档 §4.7 / 决策④）。

本文件守住四类**不会自己报错**的失败：

1. **上下文未清理导致的日志串号**：线程池把线程交给下一个请求时，
   contextvar 仍是上一个请求留下的值 —— A 的日志记到 B 的 ID 下，
   而两边各自看起来都很正常。**排障时最怕证据本身是错的。**
2. **非法 ID 被静默替换**：调用方手里的 ID 与我们库里的对不上，
   他以为能查到，实际永远查不到，且没有任何提示。
3. **日志漏带关联 ID**：做成参数的话，每一处新增调用都可能忘记传，
   表现是"日志少了一段"——不报错、不告警，只在排障时发现追不下去。
4. **关联 ID 没落库**：Worker 在**另一个进程**，contextvar 传不过去，
   只能靠 `workflow_jobs.correlation_id` 带过去，否则 Worker 的日志断了链。
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import PROJECT_ROOT
from app.context import (
    CORRELATION_ID_HEADER,
    bind_correlation_id,
    correlation_scope,
    get_correlation_id,
    is_valid_correlation_id,
    new_correlation_id,
    reset_correlation_id,
)
from app.db import Base
from app.enums import JobType, LogType
from app.main import app
from app.models import TaskLog, WorkflowJob
from app.services.log_service import LogService
from app.workflow.jobs import create_job

CLIENT = TestClient(app)
#: 不依赖数据库的端点：中间件是纯 HTTP 层的事，不该为了测它去连库
PING = "/health/live"


@pytest.fixture()
def session(work_dir) -> Session:
    """独立的库（按 ORM 元数据建表，CHECK 与新增列一并生效）。

    ⚠️ 用 `work_dir` 而**不是** pytest 的 `tmp_path`：后者的根目录在 `%TEMP%` 下，
    在部分 Windows 环境会因权限或安全软件锁抛 `PermissionError` ——
    `tests/conftest.py` 开头的说明正是为解决这件事写的，直接复用即可。
    """
    engine = create_engine(f"sqlite:///{(work_dir / 'corr.db').as_posix()}", future=True)
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db_session:
            yield db_session
    finally:
        engine.dispose()


# ============================================================
# 1. 校验与作用域（纯函数，无 IO）
# ============================================================


def test_generated_id_is_valid() -> None:
    """服务端生成的 ID 必须自己就能通过校验 —— 否则"缺省生成"这条路会自相矛盾。"""
    assert is_valid_correlation_id(new_correlation_id())


@pytest.mark.parametrize(
    "value",
    [
        "abc 123",  # 空格：进 URL 查询串会变形
        "中文标识",  # 非 ASCII
        "a" * 129,  # 超长
        "",
        "AB/CD",  # 需要转义的字符
        "AB;CD",
        "AB\nCD",
    ],
)
def test_illegal_values_are_rejected(value: str) -> None:
    assert not is_valid_correlation_id(value)
    with pytest.raises(ValueError, match="关联 ID 非法"):
        bind_correlation_id(value)


@pytest.mark.parametrize(
    "value",
    ["req-1", "a.b_c:d", "A" * 128, "00000000-0000-0000-0000-000000000000"],
)
def test_legal_values_are_accepted(value: str) -> None:
    assert is_valid_correlation_id(value)


def test_scope_restores_the_outer_value() -> None:
    """嵌套作用域退出后必须回到**外层**值，而不是清空。

    嵌套是真实存在的：请求里调服务、服务里可能再包一层
    （例如 Worker 从作业记录读回 ID 后重新绑定）。
    """
    assert get_correlation_id() is None

    with correlation_scope("outer") as outer:
        assert outer == "outer"
        assert get_correlation_id() == "outer"
        with correlation_scope("inner") as inner:
            assert inner == "inner"
            assert get_correlation_id() == "inner"
        assert get_correlation_id() == "outer", "内层退出后必须回到外层值"

    assert get_correlation_id() is None, "全部退出后必须回到未绑定"


def test_scope_resets_even_on_exception() -> None:
    """异常路径上不重置，就是日志串号的源头。"""
    with pytest.raises(RuntimeError):
        with correlation_scope("boom"):
            raise RuntimeError("boom")

    assert get_correlation_id() is None


def test_manual_bind_and_reset_are_symmetric() -> None:
    token = bind_correlation_id("manual")
    assert get_correlation_id() == "manual"
    reset_correlation_id(token)
    assert get_correlation_id() is None


# ============================================================
# 2. HTTP 中间件
# ============================================================


def test_provided_id_is_echoed_back() -> None:
    """调用方传了就用他的，并**回传** —— 他得能拿同一个 ID 来查日志。"""
    response = CLIENT.get(PING, headers={CORRELATION_ID_HEADER: "req-2026-0001"})

    assert response.status_code == 200
    assert response.headers[CORRELATION_ID_HEADER] == "req-2026-0001"


def test_missing_id_is_generated_and_echoed() -> None:
    """没传时服务端生成并回传。

    生成一个**不告诉别人**的 ID 等于没生成：调用方拿不到任何可查日志的线索。
    """
    response = CLIENT.get(PING)

    assert response.status_code == 200
    returned = response.headers[CORRELATION_ID_HEADER]
    assert returned and is_valid_correlation_id(returned)


@pytest.mark.parametrize(
    "value",
    ["abc 123", "a" * 129, "AB/CD", "AB;CD"],
)
def test_illegal_id_is_rejected_with_400(value: str) -> None:
    """**400，而不是静默替换。**

    替换之后调用方手里的 ID 与我们库里的对不上 ——
    他以为能查到，实际永远查不到，且没有任何提示。

    ⚠️ 这里**没有** `中文标识` 这类非 ASCII 取值：HTTP 头只允许 latin-1，
    httpx 在**发出去之前**就抛 `UnicodeEncodeError`，请求根本到不了我们这儿。
    也就是说非 ASCII 由传输层挡住，我们的校验不必也挡不住它 ——
    该用例在上面的 `is_valid_correlation_id` 单元测试里覆盖。
    """
    response = CLIENT.get(PING, headers={CORRELATION_ID_HEADER: value})

    assert response.status_code == 400
    assert CORRELATION_ID_HEADER not in response.headers
    body = response.json()
    assert body.get("error_code") == "INVALID_ARGUMENT" or "关联 ID" in str(body)


def test_requests_do_not_inherit_each_others_id() -> None:
    """**本文件最重要的一条**：请求之间不得串号。

    症状具体：A 带了 `req-A`，紧接着 B 不带 ID ——
    若 B 的响应头回传的是 `req-A`，说明 contextvar 没被重置，
    那个线程的下一个请求继承了上一次的值。此时 B 的日志会被记到 A 的 ID 下，
    而两边各自看起来都很正常。
    """
    client = TestClient(app)

    first = client.get(PING, headers={CORRELATION_ID_HEADER: "req-A"})
    second = client.get(PING)
    third = client.get(PING)

    assert first.headers[CORRELATION_ID_HEADER] == "req-A"
    assert second.headers[CORRELATION_ID_HEADER] != "req-A", "第二个请求继承了上一个的 ID"
    assert third.headers[CORRELATION_ID_HEADER] not in {"req-A", second.headers[CORRELATION_ID_HEADER]}


def test_scope_is_clean_outside_a_request() -> None:
    """请求之外（CLI / 测试）必须是未绑定，而不是残留某个请求的值。"""
    CLIENT.get(PING, headers={CORRELATION_ID_HEADER: "req-outside"})

    assert get_correlation_id() is None


# ============================================================
# 3. 日志与作业自动携带
# ============================================================


def test_log_carries_correlation_id_automatically(session: Session) -> None:
    """`LogService` 不接受该参数，却必须带上 —— 这正是"漏不掉"的实现方式。"""
    with correlation_scope("req-log-1"):
        entry = LogService(session).log(log_type=LogType.PULL, message="拉取完成")
        session.commit()

    stored = session.get(TaskLog, entry.id)
    assert stored.correlation_id == "req-log-1"


def test_log_without_scope_stores_none(session: Session) -> None:
    """未绑定时写 `None`，**不凭空生成**。

    生成的话，每次调用得到不同的值 —— "同一次请求的全链路"被拆成互不相干的碎片，
    而且看不出发生过这件事。
    """
    entry = LogService(session).log(log_type=LogType.PULL, message="无上下文")
    session.commit()

    assert session.get(TaskLog, entry.id).correlation_id is None


def test_job_records_correlation_id(session: Session) -> None:
    """作业记录必须落库 —— Worker 在**另一个进程**，contextvar 传不过去。"""
    with correlation_scope("req-job-1"):
        job, created = create_job(
            session,
            job_type=JobType.PULL,
            idempotency_key="pull:v1",
            input_payload={"provider": "mock", "tenant_id": "default"},
        )
        session.commit()

    assert created is True
    assert session.get(WorkflowJob, job.id).correlation_id == "req-job-1"


def test_worker_reads_back_and_rebinds(session: Session) -> None:
    """Worker 侧的完整动作：从作业记录**读回** → 重新绑定 → 日志自动带上。

    这条把"跨进程传递"这件事走了一遍：`workflow_jobs.correlation_id` 是桥，
    `correlation_scope` 是落点。缺任何一半，Worker 的日志都是断链的。
    """
    with correlation_scope("req-worker-1"):
        job, _ = create_job(
            session,
            job_type=JobType.PULL,
            idempotency_key="pull:v2",
            input_payload={"provider": "mock", "tenant_id": "default"},
        )
        session.commit()

    job_id = job.id

    # ---- 模拟另一个进程：这里没有上下文 ----
    session.expire_all()
    assert get_correlation_id() is None

    claimed = session.execute(
        select(WorkflowJob).where(WorkflowJob.id == job_id)
    ).scalar_one()

    with correlation_scope(claimed.correlation_id):
        entry = LogService(session).log(log_type=LogType.PULL, message="Worker 领取作业")
        session.commit()

    assert session.get(TaskLog, entry.id).correlation_id == "req-worker-1"


def test_correlation_ids_are_indexed(schema_conn: sqlite3.Connection) -> None:
    """两处索引都必须存在（§4.7）。

    排障的第一个动作是"按关联 ID 把一次请求的全链路捞出来"。
    没有索引时它退化成全表扫描 —— 不出错，只是在数据量上来后慢到没人再用。
    """
    indexes = {
        row[0]
        for row in schema_conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        )
    }
    assert "idx_logs_correlation" in indexes
    assert "idx_jobs_correlation" in indexes

    for table in ("task_logs", "workflow_jobs"):
        columns = {
            row[1]
            for row in schema_conn.execute(f"PRAGMA table_info({table})")
        }
        assert "correlation_id" in columns, f"{table} 缺少 correlation_id 列"


def test_schema_and_orm_agree_on_the_column() -> None:
    """`db/schema.sql`（交付脚本用）与 ORM 必须同时有这一列。

    两者只改一处时：按 schema.sql 建的库能跑，而 ORM 插入会报"没有这一列"——
    或者更糟，ORM 静默写入 `None`，于是关联 ID 永远是空的。
    """
    sql = (PROJECT_ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    assert "correlation_id" in sql
    assert "correlation_id" in TaskLog.__table__.columns
    assert "correlation_id" in WorkflowJob.__table__.columns
