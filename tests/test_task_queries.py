"""任务 / 作业 / 评价 / 结果的查询与人工确认接口（M7 / Task 3）。

## 本文件守住的是查询接口特有的那几类"不会自己报错"

1. **总数拿成了当前页的长度** —— 只有一页数据时两者永远相等，
   于是这个缺陷会一直活到数据量上来；表现是"永远只有一页"。
2. **排序不稳定** —— 同一秒创建的两行在两次请求之间换位。
   表现是翻页时**重复看到**某条、同时**漏掉**另一条，
   而两次请求各自都返回了 200 与看似正常的列表。
3. **跨租户直接猜 id** —— 403 会确认"这个 id 存在"，于是状态码本身
   成了逐位试出别人 id 的探针。本文件里枚举一整个 id 区间，
   断言响应里**没有**任何对方的数据。
4. **四态被过滤成命中** —— 只返回 `hit` 时，"这条规则为什么没报警"
   永远答不出来，而那正是四态记录存在的理由。
5. **确认有效性由前端比对摘要** —— 后端不给判据时，前端一改版就会
   静默算错，且错的方向是把**失效的确认显示成有效**。

## 为什么用 `schema.sql` 建表而不是 `Base.metadata`

后者走的是 ORM 定义，两者一旦漂移，这里测出来的约束与生产不是同一套。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import get_actor, get_db
from app.auth import Actor, Role
from app.config import PROJECT_ROOT
from app.db import transactional_session
from app.enums import ErrorCode, EvaluationStatus, TaskStatus, WriteStatus
from app.main import app
from app.models import (
    ApprovalAttachment,
    ApprovalTask,
    AuditEvent,
    CommentLog,
    ContractParse,
    ReviewResult,
    ReviewRule,
    ReviewRun,
    RuleEvaluation,
    WorkflowJob,
)

SCHEMA_FILE = PROJECT_ROOT / "db" / "schema.sql"

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


def _actor(name: str, tenant_id: str, *, roles: list[Role] | None = None) -> Actor:
    return Actor(
        actor_id=name,
        display_name=name,
        roles=frozenset(role.value for role in (roles or [Role.SYSTEM_ADMIN])),
        tenant_id=tenant_id,
    )


#: 只读审计：能读、不能确认（用来验"只读角色在写路由上 403"）。
_AUDITOR = _actor("auditor", TENANT_A, roles=[Role.READ_ONLY_AUDITOR])


# ============================================================
# 测试台
# ============================================================


class _Harness:
    def __init__(self, work_dir: Path) -> None:
        path = work_dir / "queries.db"
        conn = sqlite3.connect(path)
        try:
            conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
            conn.commit()
        finally:
            conn.close()

        self.engine = create_engine(
            f"sqlite:///{path.as_posix()}",
            future=True,
            connect_args={"check_same_thread": False},
        )
        self.factory = sessionmaker(
            bind=self.engine, autoflush=False, autocommit=False, future=True
        )
        self.client = TestClient(app)
        self.current = {"actor": _actor("admin-a", TENANT_A)}

    def install(self) -> None:
        def session_dependency():
            yield from transactional_session(self.factory())

        app.dependency_overrides[get_db] = session_dependency
        # ⚠️ 身份用一个**可变的持有者**而不是固定值：本文件要来回切换租户，
        # 而每次重建 TestClient 会让"这是同一个应用"这件事失真。
        app.dependency_overrides[get_actor] = lambda: self.current["actor"]

    def uninstall(self) -> None:
        app.dependency_overrides.clear()
        self.engine.dispose()

    def act_as(self, actor: Actor) -> None:
        self.current["actor"] = actor

    def session(self) -> Session:
        return self.factory()

    def get(self, url: str):
        return self.client.get(url)

    def post(self, url: str, json: object | None = None):
        """POST。`json=None` 表示**不带请求体**（区别于带一个空对象）。"""
        if json is None:
            return self.client.post(url)
        return self.client.post(url, json=json)

    def audit_details(self, task_id: int) -> list[dict]:
        """该任务的审计事件明细（按 id 升序）。"""
        with self.session() as session:
            rows = session.execute(
                select(AuditEvent.detail_json)
                .where(AuditEvent.task_id == task_id)
                .order_by(AuditEvent.id.asc())
            ).scalars()
            return [json.loads(raw) for raw in rows]

    # --- 种子 ---

    def seed_task(
        self,
        *,
        instance_id: str,
        tenant_id: str = TENANT_A,
        task_status: str = TaskStatus.REVIEWING.value,
        write_status: str = WriteStatus.NOT_WRITTEN.value,
        context_status: str = "complete",
        our_party_name: str = "我方公司",
        last_error_code: str | None = None,
        form_data_json: str | None = None,
        context_conflict_json: str | None = None,
    ) -> int:
        with self.session() as session:
            task = ApprovalTask(
                provider="mock",
                tenant_id=tenant_id,
                instance_id=instance_id,
                approval_code=instance_id,
                approval_title=f"{instance_id} 合同",
                task_status=task_status,
                write_status=write_status,
                context_status=context_status,
                our_party_name=our_party_name,
                our_party_contract_label="party_a",
                our_party_business_role="buyer",
                contract_type="procurement",
                last_error_code=last_error_code,
                form_data_json=form_data_json,
                context_conflict_json=context_conflict_json,
            )
            session.add(task)
            session.commit()
            return task.id

    def seed_attachment(
        self,
        task_id: int,
        *,
        attachment_id: str = "A-1",
        file_name: str = "contract.pdf",
        file_size: int = 1234,
        download_status: str = "success",
        error_message: str | None = None,
        object_key: str = "sha256/ab/cd/deadbeef.pdf",
        file_path: str = "workspace/tmp/contract.pdf",
    ) -> int:
        """一条附件记录。

        ⚠️ **刻意把 `object_key` 与 `file_path` 都写上**：它们必须存在于库里，
        而**绝不出现在任何接口响应里**（全局约束）。用"库里没有"来通过测试
        是假绿 —— 它证明的是搭桩没设值，不是接口不泄漏。
        """
        with self.session() as session:
            row = ApprovalAttachment(
                task_id=task_id,
                attachment_id=attachment_id,
                file_name=file_name,
                file_type="pdf",
                content_type="application/pdf",
                file_size=file_size,
                file_checksum="b" * 64,
                object_key=object_key,
                file_path=file_path,
                download_status=download_status,
                error_message=error_message,
            )
            session.add(row)
            session.commit()
            return row.id

    def seed_job(
        self,
        task_id: int,
        *,
        job_type: str = "parse",
        job_status: str = "queued",
        correlation_id: str | None = "corr-1",
    ) -> int:
        with self.session() as session:
            job = WorkflowJob(
                task_id=task_id,
                job_type=job_type,
                job_status=job_status,
                idempotency_key=f"job-{task_id}-{job_type}-{correlation_id}",
                input_json="{}",
                # `input_digest` 是 NOT NULL：它是"同样的输入"这条判据的一部分，
                # 空值会让幂等键失去一半含义（同键不同输入也能算命中）。
                input_digest=hashlib.sha256(b"{}").hexdigest(),
                correlation_id=correlation_id,
            )
            session.add(job)
            session.commit()
            return job.id

    def seed_parse(self, task_id: int, *, parse_version: int = 1) -> int:
        with self.session() as session:
            attachment = ApprovalAttachment(
                task_id=task_id,
                # 同一任务多次解析会有多个附件行（版本列表的用例需要）
                attachment_id=f"A-{task_id}-{parse_version}",
                file_name="contract.pdf",
                content_type="application/pdf",
                download_status="success",
                object_key=f"sha256/{task_id}-{parse_version}.pdf",
                file_checksum="a" * 64,
            )
            session.add(attachment)
            session.flush()
            parse = ContractParse(
                task_id=task_id,
                attachment_id=attachment.id,
                parse_status="succeeded",
                parse_version=parse_version,
            )
            session.add(parse)
            session.commit()
            return parse.id

    def seed_run(self, task_id: int, parse_id: int) -> int:
        with self.session() as session:
            run = ReviewRun(
                task_id=task_id,
                parse_id=parse_id,
                version_no=1,
                run_status="completed",
            )
            session.add(run)
            session.commit()
            return run.id

    def seed_rule(self, rule_code: str) -> int:
        with self.session() as session:
            rule = ReviewRule(
                rule_code=rule_code,
                rule_name=f"{rule_code} 名称",
                rule_category="自动续约",
                risk_level="medium",
                rule_status="active",
                priority=10,
                rule_version=1,
                match_mode="keyword",
                match_text='{"keywords": ["自动续约"]}',
            )
            session.add(rule)
            session.commit()
            return rule.id

    def seed_evaluation(
        self, *, task_id: int, run_id: int, rule_id: int, status: str, risk: str = "medium"
    ) -> int:
        with self.session() as session:
            row = RuleEvaluation(
                run_id=run_id,
                task_id=task_id,
                rule_id=rule_id,
                rule_version=1,
                risk_level=risk,
                evaluation_status=status,
                reason_code="CONDITION_MATCHED",
                reason_text="说明",
            )
            session.add(row)
            session.commit()
            return row.id

    def seed_result(
        self,
        *,
        task_id: int,
        run_id: int,
        version_no: int = 1,
        risk_level: str = "low",
    ) -> int:
        with self.session() as session:
            row = ReviewResult(
                task_id=task_id,
                run_id=run_id,
                overall_risk_level=risk_level,
                summary_text="摘要",
                focus_points_json="[]",
                comment_text="正文",
                review_status="complete",
                version_no=version_no,
                result_fingerprint=f"fp-{task_id}-{version_no}",
                content_digest="b" * 64,
            )
            session.add(row)
            session.commit()
            return row.id

    def seed_attempt(
        self,
        *,
        task_id: int,
        result_id: int,
        attempt_no: int,
        write_status: str,
        reason_code: str | None = None,
        reason_text: str | None = None,
    ) -> int:
        with self.session() as session:
            row = CommentLog(
                task_id=task_id,
                review_id=result_id,
                write_status=write_status,
                reason_code=reason_code,
                reason_text=reason_text,
                content_digest="b" * 64,
                idempotency_key=f"wb-{task_id}-{result_id}-{attempt_no}",
                attempt_no=attempt_no,
                operator_name="张三",
            )
            session.add(row)
            session.commit()
            return row.id

    def set_created_at(self, table: str, value: str) -> None:
        """把整张表的 `created_at` 抹成同一个值（模拟"同一秒创建"）。"""
        with self.session() as session:
            session.execute(text(f"UPDATE {table} SET created_at = '{value}'"))
            session.commit()

    def audit_actions(self, task_id: int) -> list[str]:
        with self.session() as session:
            return list(
                session.execute(
                    select(AuditEvent.action).where(AuditEvent.task_id == task_id)
                ).scalars()
            )


@pytest.fixture()
def harness(work_dir: Path):
    built = _Harness(work_dir)
    built.install()
    try:
        yield built
    finally:
        built.uninstall()


# ============================================================
# 1. 分页与总数（后端给）
# ============================================================


def test_totals_come_from_the_backend_not_from_the_page_length(harness: _Harness) -> None:
    """**验收**：`total` 是满足条件的行数，不是这一页的长度。

    只数当前页时，"共 2 条"配着 3 条数据 —— 而它在数据少于一页时
    永远是对的，因此这个缺陷会一直活到上线。
    """
    for index in range(3):
        harness.seed_task(instance_id=f"HT-{index}")

    first = harness.get("/api/tasks?page=1&page_size=2").json()

    assert first["total"] == 3
    assert first["page_count"] == 2
    assert first["has_next"] is True
    assert len(first["items"]) == 2

    second = harness.get("/api/tasks?page=2&page_size=2").json()

    assert second["total"] == 3
    assert second["page_count"] == 2
    assert second["has_next"] is False
    assert len(second["items"]) == 1


def test_the_two_pages_do_not_overlap_and_cover_everything(harness: _Harness) -> None:
    """分页不漏不重 —— 这是"排序稳定"唯一可观测的含义。"""
    for index in range(5):
        harness.seed_task(instance_id=f"HT-{index}")

    seen = []
    for page in (1, 2, 3):
        body = harness.get(f"/api/tasks?page={page}&page_size=2").json()
        seen.extend(item["task_id"] for item in body["items"])

    assert len(seen) == len(set(seen)) == 5


def test_identical_created_at_still_gives_a_total_order(harness: _Harness) -> None:
    """同一秒创建的行也必须有一个**全序**。

    ⚠️ 只按 `created_at` 排序时，这四行在两次请求之间可以换位 ——
    于是翻到第 2 页会**再看到一次**第 1 页的某条，同时**漏掉**另一条。
    SQLite 的 `CURRENT_TIMESTAMP` 精度只到秒，这不是理论问题。
    """
    for index in range(4):
        harness.seed_task(instance_id=f"HT-{index}")
    harness.set_created_at("approval_tasks", "2026-09-15 10:00:00")

    first = [item["task_id"] for item in harness.get("/api/tasks?page_size=4").json()["items"]]
    second = [item["task_id"] for item in harness.get("/api/tasks?page_size=4").json()["items"]]

    assert first == second, "同样的请求必须给出同样的顺序"
    assert first == sorted(first, reverse=True), "`id` 必须是排序的一部分（倒序）"


def test_an_unknown_filter_value_is_rejected_instead_of_returning_nothing(
    harness: _Harness,
) -> None:
    """拼错的过滤值 → **400**，不是空列表。

    空列表会被读成"没有阻塞的任务"，而真相是"这个过滤值从来不存在"。
    两者在响应里长得一模一样，因此必须在这里断开。
    """
    harness.seed_task(instance_id="HT-1", task_status=TaskStatus.BLOCKED.value)

    ok = harness.get("/api/tasks?task_status=blocked")
    assert ok.status_code == 200
    assert ok.json()["total"] == 1

    bad = harness.get("/api/tasks?task_status=blockd")
    assert bad.status_code == 400


# ============================================================
# 2. 任务详情：任务级与尝试级的两个回写口径（缺口 G-4）
# ============================================================


def test_task_detail_reports_both_writeback_levels(harness: _Harness) -> None:
    """任务级说"写成了没有"，尝试级说"最近一次为什么没成"。"""
    task_id = harness.seed_task(instance_id="HT-1")
    result_id = harness.seed_result(task_id=task_id, run_id=harness.seed_run(task_id, harness.seed_parse(task_id)))
    harness.seed_attempt(
        task_id=task_id,
        result_id=result_id,
        attempt_no=1,
        write_status=WriteStatus.NOT_WRITTEN.value,
        reason_code="MANUAL_CONFIRM_REQUIRED",
        reason_text="高风险尚未人工确认",
    )

    body = harness.get(f"/api/tasks/{task_id}").json()
    writeback = body["writeback"]

    assert body["task_status"] == TaskStatus.REVIEWING.value
    assert writeback["task_write_status"] == WriteStatus.NOT_WRITTEN.value
    assert writeback["latest_attempt_no"] == 1
    assert writeback["latest_attempt_status"] == WriteStatus.NOT_WRITTEN.value
    assert writeback["latest_reason_code"] == "MANUAL_CONFIRM_REQUIRED"
    assert writeback["latest_reason_text"] == "高风险尚未人工确认"
    assert writeback["latest_attempt_rejected"] is True


def test_latest_attempt_is_the_highest_attempt_no(harness: _Harness) -> None:
    """重试之后，"最近一次"要跟着走 —— 而原因要带上**是第几次的**原因。

    ⚠️ 只给原因不给序号时，一个重试过的任务在界面上无法回答
    「这是哪一次的原因」。
    """
    task_id = harness.seed_task(instance_id="HT-1")
    result_id = harness.seed_result(task_id=task_id, run_id=harness.seed_run(task_id, harness.seed_parse(task_id)))
    harness.seed_attempt(
        task_id=task_id,
        result_id=result_id,
        attempt_no=1,
        write_status=WriteStatus.NOT_WRITTEN.value,
        reason_code="MANUAL_CONFIRM_REQUIRED",
    )
    harness.seed_attempt(
        task_id=task_id,
        result_id=result_id,
        attempt_no=2,
        write_status=WriteStatus.FAILED.value,
        reason_code="APPROVAL_API_ERROR",
    )

    writeback = harness.get(f"/api/tasks/{task_id}").json()["writeback"]

    assert writeback["latest_attempt_no"] == 2
    assert writeback["latest_attempt_status"] == WriteStatus.FAILED.value
    assert writeback["latest_reason_code"] == "APPROVAL_API_ERROR"
    assert writeback["latest_attempt_rejected"] is False, "外部失败不是「被拒」，两者的处置方向相反"


def test_task_detail_exposes_the_correlation_id_and_chain(harness: _Harness) -> None:
    """关联 ID 与链路指针：排障时要能拿着 ID 去查日志。"""
    task_id = harness.seed_task(instance_id="HT-1")
    parse_id = harness.seed_parse(task_id)
    run_id = harness.seed_run(task_id, parse_id)
    harness.seed_job(task_id, correlation_id="corr-abc")

    body = harness.get(f"/api/tasks/{task_id}").json()

    assert body["correlation_id"] == "corr-abc"
    assert body["latest_parse_id"] == parse_id
    assert body["latest_run_id"] == run_id
    assert body["attachment_count"] == 1


def test_task_without_jobs_reports_no_correlation_id(harness: _Harness) -> None:
    """没有作业时 `correlation_id` 是 `None`。

    ⚠️ 不能编一个出来：排障的人会拿着一个**查不到任何日志**的 ID 去查，
    而"查不到"会被读成"日志丢了" —— 真正的事实是"这件事还没发生过"。
    """
    task_id = harness.seed_task(instance_id="HT-1")

    assert harness.get(f"/api/tasks/{task_id}").json()["correlation_id"] is None


# ============================================================
# 3. 规则评价：四态可见 + 需关注的排前
# ============================================================


def test_evaluations_keep_all_four_states_and_order_attention_first(
    harness: _Harness,
) -> None:
    """**验收**：四态都给，且默认把 `hit` 与 `needs_review` 排前面。

    只返回 `hit` 时，"这条规则为什么没报警"永远答不出来 ——
    而那正是四态记录存在的理由。
    """
    task_id = harness.seed_task(instance_id="HT-1")
    parse_id = harness.seed_parse(task_id)
    run_id = harness.seed_run(task_id, parse_id)
    for code, status in (
        ("R_NOT_HIT", EvaluationStatus.NOT_HIT.value),
        ("R_HIT", EvaluationStatus.HIT.value),
        ("R_NA", EvaluationStatus.NOT_APPLICABLE.value),
        ("R_REVIEW", EvaluationStatus.NEEDS_REVIEW.value),
    ):
        harness.seed_evaluation(
            task_id=task_id,
            run_id=run_id,
            rule_id=harness.seed_rule(code),
            status=status,
        )

    body = harness.get(f"/api/evaluations?run_id={run_id}").json()

    assert body["total"] == 4, "四态一条都不能少"
    assert {item["evaluation_status"] for item in body["items"]} == {
        "hit",
        "not_hit",
        "not_applicable",
        "needs_review",
    }

    statuses = [item["evaluation_status"] for item in body["items"]]
    assert statuses[0] == "hit"
    assert statuses[1] == "needs_review"
    assert set(statuses[2:]) == {"not_hit", "not_applicable"}


def test_evaluation_ordering_is_stable_within_the_same_weight(
    harness: _Harness,
) -> None:
    """同权重内按 `rule_code` 稳定排序 —— 否则翻页会重复或漏行。"""
    task_id = harness.seed_task(instance_id="HT-1")
    run_id = harness.seed_run(task_id, harness.seed_parse(task_id))
    for code in ("R_C", "R_A", "R_B"):
        harness.seed_evaluation(
            task_id=task_id,
            run_id=run_id,
            rule_id=harness.seed_rule(code),
            status=EvaluationStatus.NOT_HIT.value,
        )

    first = [item["rule_code"] for item in harness.get("/api/evaluations").json()["items"]]
    second = [item["rule_code"] for item in harness.get("/api/evaluations").json()["items"]]

    assert first == second == ["R_A", "R_B", "R_C"]


# ============================================================
# 4. 结果：`confirmation_valid` 由后端给
# ============================================================


def test_confirmation_valid_is_computed_by_the_backend(harness: _Harness) -> None:
    """**验收**：确认有效性由后端判定，前端不必比对两个摘要。

    ⚠️ 只比对摘要（`content_digest == confirmed_digest`）会漏掉**版本接替** ——
    而"旧版本的确认还有效"正是那个错觉的来源。
    """
    task_id = harness.seed_task(instance_id="HT-1")
    run_id = harness.seed_run(task_id, harness.seed_parse(task_id))
    result_id = harness.seed_result(task_id=task_id, run_id=run_id)

    before = harness.get(f"/api/results/{result_id}").json()
    assert before["manual_confirmed"] == 0
    assert before["confirmation_valid"] is False
    assert before["is_current_version"] is True

    harness.act_as(_actor("reviewer-1", TENANT_A, roles=[Role.LEGAL_REVIEWER]))
    confirmed = harness.post(f"/api/results/{result_id}/confirm").json()
    assert confirmed["manual_confirmed"] == 1
    assert confirmed["confirmed_by"] == "reviewer-1"
    assert confirmed["confirmation_valid"] is True

    # 新版本出现 → 旧版本的确认**立即失效**（字段原样保留，判据变了）
    harness.act_as(_actor("admin-a", TENANT_A))
    harness.seed_result(task_id=task_id, run_id=run_id, version_no=2)

    stale = harness.get(f"/api/results/{result_id}").json()
    assert stale["manual_confirmed"] == 1, "历史不删"
    assert stale["is_current_version"] is False
    assert stale["confirmation_valid"] is False


def test_result_list_rows_have_the_same_shape_as_the_detail(harness: _Harness) -> None:
    """列表行与详情**逐字同形** —— 否则界面要为每一行再发一次详情请求。"""
    task_id = harness.seed_task(instance_id="HT-1")
    run_id = harness.seed_run(task_id, harness.seed_parse(task_id))
    result_id = harness.seed_result(task_id=task_id, run_id=run_id)

    row = harness.get("/api/results").json()["items"][0]
    detail = harness.get(f"/api/results/{result_id}").json()

    assert row == detail


# ============================================================
# 5. 租户可见性：每一次查询、含嵌套 id
# ============================================================


@pytest.fixture()
def two_tenants(harness: _Harness) -> dict[str, int]:
    """租户 A 的一条完整链路，返回各层 id。"""
    task_id = harness.seed_task(instance_id="HT-A", tenant_id=TENANT_A)
    parse_id = harness.seed_parse(task_id)
    run_id = harness.seed_run(task_id, parse_id)
    result_id = harness.seed_result(task_id=task_id, run_id=run_id)
    job_id = harness.seed_job(task_id)
    attempt_id = harness.seed_attempt(
        task_id=task_id,
        result_id=result_id,
        attempt_no=1,
        write_status=WriteStatus.WRITING.value,
    )
    return {
        "task_id": task_id,
        "parse_id": parse_id,
        "run_id": run_id,
        "result_id": result_id,
        "job_id": job_id,
        "attempt_id": attempt_id,
    }


def test_another_tenant_cannot_read_a_nested_resource_by_guessing_its_id(
    harness: _Harness, two_tenants: dict[str, int]
) -> None:
    """**验收**：跨租户直接猜 id → 404，且响应里**没有**对方的数据。

    逐层都试一遍：只堵住任务列表时，`job_id` / `parse_id` / `result_id`
    仍会安静地返回别人的数据 —— 它们单独看都不敏感，
    但合起来能拼出对方的数据结构。
    """
    harness.act_as(_actor("admin-b", TENANT_B))

    urls = {
        "task": f"/api/tasks/{two_tenants['task_id']}",
        "job": f"/api/jobs/{two_tenants['job_id']}",
        "parse": f"/api/parses/{two_tenants['parse_id']}",
        "run": f"/api/runs/{two_tenants['run_id']}",
        "result": f"/api/results/{two_tenants['result_id']}",
        "writeback": f"/api/writebacks/{two_tenants['attempt_id']}",
    }

    for label, url in urls.items():
        response = harness.get(url)
        assert response.status_code == 404, f"{label} 不应跨租户可见：{response.text}"
        # 不泄漏：对方的业务字段一个都不能出现在响应体里
        assert "HT-A" not in response.text
        assert "我方公司" not in response.text


def test_enumeration_over_an_id_range_yields_no_data(
    harness: _Harness, two_tenants: dict[str, int]
) -> None:
    """**验收**：枚举一整个 id 区间，全部 404 且内容一致。

    ⚠️ 断言"全都 404"还不够 —— 还要断言**它们彼此相同**。
    只要有一条返回 403（"存在但不属于你"），状态码本身就成了探针：
    攻击者据此能确定哪些 id 是真实存在的。
    """
    harness.act_as(_actor("admin-b", TENANT_B))

    responses = [
        harness.get(f"/api/tasks/{task_id}") for task_id in range(1, 12)
    ]

    assert {response.status_code for response in responses} == {404}
    bodies = {response.text for response in responses}
    assert len(bodies) == 1 or all(
        "不存在" in body for body in bodies
    ), "跨租户与不存在必须不可区分"


def test_lists_never_mix_tenants(harness: _Harness, two_tenants: dict[str, int]) -> None:
    """列表接口同样要过租户门 —— 单条详情堵住了，列表照样能拖出全量。"""
    harness.seed_task(instance_id="HT-B", tenant_id=TENANT_B)
    harness.act_as(_actor("admin-b", TENANT_B))

    tasks = harness.get("/api/tasks").json()
    assert tasks["total"] == 1
    assert [item["instance_id"] for item in tasks["items"]] == ["HT-B"]

    assert harness.get("/api/results").json()["total"] == 0
    assert harness.get("/api/evaluations").json()["total"] == 0
    assert harness.get("/api/jobs").json()["total"] == 0


def test_a_cross_tenant_result_cannot_be_confirmed(
    harness: _Harness, two_tenants: dict[str, int]
) -> None:
    """写路由也要过租户门：确认自己的结果没问题，确认别人的 404。

    只在读路由上过滤时，攻击者可以**替对方确认**一份结果 ——
    而回写门禁随后会把它当成"已人工确认"放行。
    """
    harness.act_as(_actor("reviewer-b", TENANT_B, roles=[Role.LEGAL_REVIEWER]))

    response = harness.post(f"/api/results/{two_tenants['result_id']}/confirm")

    assert response.status_code == 404
    harness.act_as(_actor("admin-a", TENANT_A))
    assert harness.get(f"/api/results/{two_tenants['result_id']}").json()[
        "manual_confirmed"
    ] == 0


# ============================================================
# 6. 立场确认
# ============================================================


def test_context_confirmation_requires_a_complete_context(harness: _Harness) -> None:
    """只有 `complete` 可确认。

    `missing` / `conflict` 状态下**没有可确认的对象**：前者我方名称都还没有，
    后者两个来源互相矛盾。放行等于让人确认一个我们说不清是什么的东西，
    而回写门禁随后会把它当成可信立场使用。
    """
    task_id = harness.seed_task(instance_id="HT-1", context_status="complete")
    ok = harness.post(f"/api/tasks/{task_id}/context/confirm")
    assert ok.status_code == 200
    assert ok.json()["context_status"] == "confirmed"

    for bad_status in ("missing", "conflict"):
        other = harness.seed_task(instance_id=f"HT-{bad_status}", context_status=bad_status)
        response = harness.post(f"/api/tasks/{other}/context/confirm")
        assert response.status_code == 400
        assert "complete" in response.text, "错误消息要说清「只有 complete 可确认」"


def test_context_confirmation_is_idempotent_and_audited_once(harness: _Harness) -> None:
    """重复确认是**一次**业务事实：不追加审计事件。

    审计账要能按动作聚合统计（"本周确认了多少份立场"）。
    重复计数会让这个数字直接失真 —— 而账面上看不出哪些是重复的。
    """
    task_id = harness.seed_task(instance_id="HT-1", context_status="complete")

    first = harness.post(f"/api/tasks/{task_id}/context/confirm").json()
    second = harness.post(f"/api/tasks/{task_id}/context/confirm").json()

    assert first == second
    assert harness.audit_actions(task_id) == ["CONTEXT_CONFIRMED"]


def test_context_confirmation_marks_the_source_as_manual(harness: _Harness) -> None:
    """确认后 `context_source` 变成 `manual`。

    留成 `approval_system` 时，事后无法区分"审批系统给的就是这样"
    与"人工看过并认可" —— 而门禁把 `confirmed` 当可信立场的理由恰恰是后者。
    """
    task_id = harness.seed_task(instance_id="HT-1", context_status="complete")

    body = harness.post(f"/api/tasks/{task_id}/context/confirm").json()

    assert body["context_source"] == "manual"


def test_read_only_auditor_cannot_confirm_anything(harness: _Harness) -> None:
    """**验收**：只读角色在写路由上被后端拒绝（403），逐条证明。

    前端隐藏按钮不算数 —— 前端被绕过时，后端是唯一还站着的那道门。
    """
    task_id = harness.seed_task(instance_id="HT-1", context_status="complete")
    run_id = harness.seed_run(task_id, harness.seed_parse(task_id))
    result_id = harness.seed_result(task_id=task_id, run_id=run_id)

    harness.act_as(_AUDITOR)

    assert harness.post(f"/api/tasks/{task_id}/context/confirm").status_code == 403
    assert harness.post(f"/api/results/{result_id}/confirm").status_code == 403
    # 读仍然是允许的：只读角色名不副实也是缺陷
    assert harness.get(f"/api/tasks/{task_id}").status_code == 200


# ============================================================
# 7. 路由守卫
# ============================================================


def test_no_route_path_is_registered_twice() -> None:
    """同一个路径 + 方法不得注册两次。

    ⚠️ 后注册的那一条**永远不生效且不报错** —— 一个只在删掉前一条时
    才被发现的分叉。M7 把 `/api/results/{id}` 从 `jobs` 挪到 `results`
    时正是靠这条守住的。
    """
    seen: dict[tuple[str, str], int] = {}
    for route in app.routes:
        path = getattr(route, "path", None)
        if not path or not path.startswith("/api"):
            continue
        for method in getattr(route, "methods", None) or ():
            key = (method, path)
            seen[key] = seen.get(key, 0) + 1

    duplicates = {key: count for key, count in seen.items() if count > 1}
    assert duplicates == {}, f"路由被注册了不止一次：{duplicates}"


# ============================================================
# 8. 汇总计数（M8 缺口 G-M8-1）
# ============================================================
# 卡片说的是**全量**，而前端只能数到当前这一页 —— 数据少于一页时两者
# 永远相等，因此"前端自己数"这个缺陷会一直活到上线之后。


def test_summary_counts_more_than_one_page(harness: _Harness) -> None:
    """汇总计的是全量，不是某页的长度。"""
    for index in range(3):
        harness.seed_task(instance_id=f"HT-P-{index}", task_status=TaskStatus.PENDING.value)
    for index in range(2):
        harness.seed_task(
            instance_id=f"HT-B-{index}", task_status=TaskStatus.BLOCKED.value
        )

    body = harness.get("/api/tasks/summary").json()

    assert body["total"] == 5
    assert body["by_status"]["pending"] == 3
    assert body["by_status"]["blocked"] == 2
    # 分页是 1 条时，"数这一页"会得到 1
    assert harness.get("/api/tasks?page_size=1").json()["items"].__len__() == 1


def test_summary_keys_are_always_complete(harness: _Harness) -> None:
    """五个状态的键**恒定齐全**（为 0 也要在）。

    缺键时前端渲染出 `undefined`，而"0"与"没有这个键"在界面上分不开 ——
    于是"这个状态一条都没有"看起来像"这个字段坏了"。
    """
    harness.seed_task(instance_id="HT-ONLY", task_status=TaskStatus.DONE.value)

    by_status = harness.get("/api/tasks/summary").json()["by_status"]

    assert set(by_status) == {status.value for status in TaskStatus}
    assert by_status["done"] == 1
    assert by_status["pending"] == 0


def test_summary_is_scoped_to_the_tenant(harness: _Harness) -> None:
    """汇总不把别的租户数进来。"""
    harness.seed_task(instance_id="HT-A", tenant_id=TENANT_A)
    for index in range(4):
        harness.seed_task(instance_id=f"HT-B-{index}", tenant_id=TENANT_B)

    assert harness.get("/api/tasks/summary").json()["total"] == 1

    harness.act_as(_actor("admin-b", TENANT_B))
    assert harness.get("/api/tasks/summary").json()["total"] == 4


def test_writeback_failed_count_uses_the_task_level_status(harness: _Harness) -> None:
    """回写失败数取**任务级** `approval_tasks.write_status`（口径 G-4）。

    ⚠️ 不是"有几条失败的尝试"：任务是可能被重试的，
    一次失败尝试之后又成功了，这条任务**不该**算进"回写失败"。
    两个口径混用会让卡片数字与列表里那一列对不上。
    """
    failed = harness.seed_task(instance_id="HT-F", write_status=WriteStatus.FAILED.value)
    recovered = harness.seed_task(instance_id="HT-R", write_status=WriteStatus.SUCCESS.value)
    # 这条任务**任务级**是成功，但历史上有一条失败尝试
    parse_id = harness.seed_parse(recovered)
    run_id = harness.seed_run(recovered, parse_id)
    result_id = harness.seed_result(task_id=recovered, run_id=run_id)
    harness.seed_attempt(
        task_id=recovered,
        result_id=result_id,
        attempt_no=1,
        write_status=WriteStatus.FAILED.value,
        reason_code="APPROVAL_API_ERROR",
    )
    assert failed > 0

    body = harness.get("/api/tasks/summary").json()

    assert body["writeback_failed"] == 1


def test_summary_requires_task_read(harness: _Harness) -> None:
    """汇总需要 `task:read`；一个角色都没有的主体被拒。

    ⚠️ 这里**直接构造 `Actor`**，不走 `_actor(...)` 助手：那个助手的
    `roles or [Role.SYSTEM_ADMIN]` 在传空列表时会**回落成管理员** ——
    于是"一个角色都没有"的用例实际上测的是管理员，而它照样是绿的。
    """
    harness.act_as(
        Actor(
            actor_id="nobody",
            display_name="nobody",
            roles=frozenset(),
            tenant_id=TENANT_A,
        )
    )

    assert harness.get("/api/tasks/summary").status_code == 403


def test_summary_route_is_not_swallowed_by_the_task_id_route(harness: _Harness) -> None:
    """`/tasks/summary` 必须**先**于 `/tasks/{task_id}` 注册。

    ⚠️ 反过来时它不会报错，只会**静默地永远不生效**：请求被通配路由吃掉，
    得到 422（`summary` 不是合法 int）。那种失败看起来像"参数传错了"，
    而真相是"这条路由从来没被匹配到过"。
    """
    harness.seed_task(instance_id="HT-1")

    response = harness.get("/api/tasks/summary")

    assert response.status_code == 200, response.text
    assert "by_status" in response.json()


# ============================================================
# 9. 列表的总风险等级（M8 缺口 G-M8-3）
# ============================================================


def test_list_and_detail_agree_on_the_risk_level(harness: _Harness) -> None:
    """列表行与详情给**同一个**风险等级（判据只留一份）。"""
    task_id = harness.seed_task(instance_id="HT-R")
    parse_id = harness.seed_parse(task_id)
    run_id = harness.seed_run(task_id, parse_id)
    harness.seed_result(task_id=task_id, run_id=run_id, risk_level="high")

    row = harness.get("/api/tasks").json()["items"][0]
    detail = harness.get(f"/api/tasks/{task_id}").json()

    assert row["overall_risk_level"] == "high"
    assert detail["overall_risk_level"] == row["overall_risk_level"]


def test_no_result_means_no_risk_level_not_low(harness: _Harness) -> None:
    """没审查过的任务给 `null`，**不是** `low`。

    把"没审过"显示成"低风险"是最危险的一种默认值：它让一份**从未被审查**的合同
    看起来是安全的。
    """
    harness.seed_task(instance_id="HT-NEW")

    row = harness.get("/api/tasks").json()["items"][0]

    assert row["overall_risk_level"] is None
    assert row["overall_risk_level"] != "low"


def test_risk_level_follows_the_current_version(harness: _Harness) -> None:
    """取**当前版本**的风险等级（高版本覆盖低版本）。"""
    task_id = harness.seed_task(instance_id="HT-V")
    parse_id = harness.seed_parse(task_id)
    run_id = harness.seed_run(task_id, parse_id)
    harness.seed_result(
        task_id=task_id, run_id=run_id, version_no=1, risk_level="high"
    )
    harness.seed_result(
        task_id=task_id, run_id=run_id, version_no=2, risk_level="low"
    )

    row = harness.get("/api/tasks").json()["items"][0]

    assert row["overall_risk_level"] == "low"


def test_list_row_carries_the_writeback_status_and_reason(harness: _Harness) -> None:
    """列表行同时给**任务级状态**与**最近一次尝试的原因**（设计 §5.1）。

    只给状态时，`写失败` 与 `未回写（被门禁拒绝）` 在列表上长得一样 ——
    而两者的处置相反：前者去重试，后者去确认（重试一个被拒的请求永远是白试）。
    """
    task_id = harness.seed_task(instance_id="HT-W", write_status=WriteStatus.NOT_WRITTEN.value)
    parse_id = harness.seed_parse(task_id)
    run_id = harness.seed_run(task_id, parse_id)
    result_id = harness.seed_result(task_id=task_id, run_id=run_id)
    harness.seed_attempt(
        task_id=task_id,
        result_id=result_id,
        attempt_no=1,
        write_status=WriteStatus.NOT_WRITTEN.value,
        reason_code="MANUAL_CONFIRM_REQUIRED",
        reason_text="高风险结果尚未人工确认",
    )

    row = harness.get("/api/tasks").json()["items"][0]

    assert row["write_status"] == WriteStatus.NOT_WRITTEN.value
    assert row["writeback"]["task_write_status"] == WriteStatus.NOT_WRITTEN.value
    assert row["writeback"]["latest_reason_code"] == "MANUAL_CONFIRM_REQUIRED"
    # 这一位决定"该去确认还是该去重试"，因此必须由后端给出，而不是前端从原因码推
    assert row["writeback"]["latest_attempt_rejected"] is True


def test_list_and_detail_writeback_shapes_match(harness: _Harness) -> None:
    """列表行与详情的 `writeback` **键完全相同**。

    两套形状时，分叉方式是"某天只给详情加了一个字段" ——
    于是列表页永远显示不出那个信息，而没有任何地方会报错。
    """
    task_id = harness.seed_task(instance_id="HT-S")

    row = harness.get("/api/tasks").json()["items"][0]
    detail = harness.get(f"/api/tasks/{task_id}").json()

    assert set(row["writeback"]) == set(detail["writeback"])
    assert set(row) <= set(detail), "列表行的字段应当是详情字段的子集"


def test_parse_list_returns_versions_newest_first(harness: _Harness) -> None:
    """解析版本列表**新版本在前**（M8 缺口 G-M8-4 的剩余部分）。

    界面默认选中"最新那次解析"，而版本切换靠的就是这份列表。
    排序反了会让默认选中的是**最旧**的那一版 —— 而它照样能渲染，
    只是用户看到的是一份过期的字段与 PDF。
    """
    task_id = harness.seed_task(instance_id="HT-P")
    harness.seed_parse(task_id, parse_version=1)
    harness.seed_parse(task_id, parse_version=2)

    body = harness.get(f"/api/tasks/{task_id}/parses").json()

    assert [item["parse_version"] for item in body["items"]] == [2, 1]
    assert body["total"] == 2


def test_parse_list_row_shape_matches_detail(harness: _Harness) -> None:
    """列表行与详情**逐键相同**（共用一份序列化函数）。

    分成两份时，分叉方式是"某天只给详情加了一个字段" ——
    于是列表页永远显示不出那个信息，而没有任何地方会报错。
    """
    task_id = harness.seed_task(instance_id="HT-P")
    parse_id = harness.seed_parse(task_id)

    row = harness.get(f"/api/tasks/{task_id}/parses").json()["items"][0]
    detail = harness.get(f"/api/parses/{parse_id}").json()

    assert set(row) == set(detail)
    assert row == detail


def test_parse_list_is_scoped_to_the_tenant(harness: _Harness) -> None:
    """跨租户 → **404**，而不是"空列表"。

    ⚠️ 这是本文件里最要紧的一条：空列表读起来是"这个任务还没解析过"，
    而真相是"这不是你的任务"。两者在界面上都无法再分辨 ——
    于是用户会去重跑一次解析（而它永远不会出现在他的列表里）。
    """
    task_id = harness.seed_task(instance_id="HT-B", tenant_id=TENANT_B)
    harness.seed_parse(task_id)

    response = harness.get(f"/api/tasks/{task_id}/parses")

    assert response.status_code == 404
    assert response.json()["error_code"] == "RESOURCE_NOT_FOUND"


def test_parse_list_of_a_task_without_parses_is_empty_not_404(
    harness: _Harness,
) -> None:
    """**自己的**任务还没有解析记录时给空列表（200），不是 404。

    与上一条的区别是"看得见但没有"与"看不见"—— 两者的处置完全不同。
    """
    task_id = harness.seed_task(instance_id="HT-NEW")

    response = harness.get(f"/api/tasks/{task_id}/parses")

    assert response.status_code == 200
    assert response.json()["items"] == []
    assert response.json()["total"] == 0


def test_parse_list_requires_task_read(harness: _Harness) -> None:
    """解析版本列表需要 `task:read`。"""
    task_id = harness.seed_task(instance_id="HT-P")
    harness.seed_parse(task_id)
    harness.act_as(
        Actor(
            actor_id="nobody",
            display_name="nobody",
            roles=frozenset(),
            tenant_id=TENANT_A,
        )
    )

    assert harness.get(f"/api/tasks/{task_id}/parses").status_code == 403


def test_attachment_list_never_exposes_object_key_or_file_path(
    harness: _Harness,
) -> None:
    """附件列表**绝不下发** `object_key` 与 `file_path`（全局约束）。

    ⚠️ 这是本文件里最要紧的一条。两者的后果不同，但都不可接受：
    - `object_key` 是内部存储布局（`sha256/ab/cd/…`），下发会让调用端依赖具体实现，
      使 M9 换 MinIO 从"实现替换"变成"破坏性变更"；
    - `file_path` 是一条**绕过鉴权**的读取通道（可预测的服务器路径）。

    判据不只是"响应里没有这两个键"，还包括**它们的值**没有从别的字段漏出来 ——
    因此下面同时断言整份响应文本里不出现那两个值。
    """
    task_id = harness.seed_task(instance_id="HT-A")
    harness.seed_attachment(task_id, object_key="sha256/ab/cd/secret.pdf")

    response = harness.get(f"/api/tasks/{task_id}/attachments")
    row = response.json()["items"][0]

    assert response.status_code == 200
    assert "object_key" not in row
    assert "file_path" not in row
    assert "sha256/ab/cd/secret.pdf" not in response.text
    assert "workspace/tmp" not in response.text


def test_attachment_row_carries_what_the_ui_needs(harness: _Harness) -> None:
    """每行给出界面要显示的东西 + **内容地址**。"""
    task_id = harness.seed_task(instance_id="HT-A")
    record_id = harness.seed_attachment(
        task_id,
        file_name="原材料采购合同.pdf",
        file_size=2048,
        download_status="failed",
        error_message="附件已被删除",
    )

    row = harness.get(f"/api/tasks/{task_id}/attachments").json()["items"][0]

    assert row["attachment_record_id"] == record_id
    assert row["file_name"] == "原材料采购合同.pdf"
    assert row["file_size"] == 2048
    assert row["content_type"] == "application/pdf"
    # 摘要用于"我拿到的就是那一份"的核对（界面显示前 12 位）
    assert row["file_checksum"] == "b" * 64
    assert row["download_status"] == "failed"
    assert row["error_message"] == "附件已被删除"
    # 内容只在**这里**给地址：UI 不拼路径，存储实现换了它也不变
    assert row["content_url"] == f"/api/attachments/{record_id}/content"


def test_attachment_list_is_scoped_to_the_tenant(harness: _Harness) -> None:
    """跨租户 → 404，而不是空列表（"看不见"与"没有附件"必须可分辨）。"""
    task_id = harness.seed_task(instance_id="HT-B", tenant_id=TENANT_B)
    harness.seed_attachment(task_id)

    assert harness.get(f"/api/tasks/{task_id}/attachments").status_code == 404


def test_attachment_list_of_a_task_without_attachments_is_empty(
    harness: _Harness,
) -> None:
    """自己的任务没有附件时给空列表（200）。"""
    task_id = harness.seed_task(instance_id="HT-NONE")

    response = harness.get(f"/api/tasks/{task_id}/attachments")

    assert response.status_code == 200
    assert response.json()["items"] == []


def test_attachment_list_requires_task_read(harness: _Harness) -> None:
    """附件列表需要 `task:read`。"""
    task_id = harness.seed_task(instance_id="HT-A")
    harness.act_as(
        Actor(
            actor_id="nobody",
            display_name="nobody",
            roles=frozenset(),
            tenant_id=TENANT_A,
        )
    )

    assert harness.get(f"/api/tasks/{task_id}/attachments").status_code == 403


def test_task_detail_carries_form_data(harness: _Harness) -> None:
    """详情带上**审批表单**（原样键值对）。"""
    task_id = harness.seed_task(
        instance_id="HT-F",
        form_data_json=json.dumps({"金额": "1200000", "供应商": "某某公司"}),
    )

    body = harness.get(f"/api/tasks/{task_id}").json()

    assert body["form_data"] == {"金额": "1200000", "供应商": "某某公司"}


def test_business_fact_flag_separates_external_facts_from_system_faults(
    harness: _Harness,
) -> None:
    """`last_error_is_business_fact` 区分**业务结论**与**系统故障**（§5.2）。

    | 错误码 | 含义 | 界面 |
    | --- | --- | --- |
    | `ATTACHMENT_MISSING` | 外部系统说附件没了（业务事实） | 黄色，去找上传人 |
    | `STORAGE_UNAVAILABLE` | 存储抖动（系统故障） | 红色，等自动重试 |

    两者都渲染成"失败"时，用户会对同一句话采取错误的动作 ——
    而正确处置是相反的（找人 vs 等系统）。

    ⚠️ 判据在**服务端**（`app/errors.py::BUSINESS_FACT_CODES`），
    前端不另列一张码表：那样两张表迟早漂移。
    """
    from app.errors import BUSINESS_FACT_CODES

    missing = harness.seed_task(
        instance_id="HT-M", last_error_code=ErrorCode.ATTACHMENT_MISSING.value
    )
    storage = harness.seed_task(
        instance_id="HT-S", last_error_code=ErrorCode.STORAGE_UNAVAILABLE.value
    )
    clean = harness.seed_task(instance_id="HT-C")

    assert harness.get(f"/api/tasks/{missing}").json()["last_error_is_business_fact"] is True
    assert harness.get(f"/api/tasks/{storage}").json()["last_error_is_business_fact"] is False
    # 没有错误码时既不是业务事实也不是故障
    assert harness.get(f"/api/tasks/{clean}").json()["last_error_is_business_fact"] is False
    # 这条码表与响应口径同源（改了这里就该改判定）
    assert ErrorCode.ATTACHMENT_MISSING in BUSINESS_FACT_CODES


_CORRECTION = {
    "our_party_name": "某某科技有限公司",
    "our_party_contract_label": "party_a",
    "our_party_business_role": "buyer",
    "contract_type": "procurement",
}


def test_missing_context_cannot_be_confirmed_without_a_correction(
    harness: _Harness,
) -> None:
    """**不带请求体**时，`missing` 不能确认（没有可确认的对象）。

    这是正确的约束，也正是"必须提供修正"的原因 —— 下一条用例证明
    带上四条业务事实之后这条路是通的。
    """
    task_id = harness.seed_task(instance_id="HT-M", context_status="missing")

    response = harness.post(f"/api/tasks/{task_id}/context/confirm")

    assert response.status_code == 400
    assert "修正" in response.json()["message"]


def test_missing_context_is_resolved_by_a_correction(harness: _Harness) -> None:
    """**缺口 G-M8-7 的验收**：`missing` 状态下人工给出四值即可确认。

    没有这条路时，`missing`（刚拉取完任务的**正常**状态）会永久停住：
    确认被拒、又没有入口能填进去，而回写门禁要求可信立场。
    """
    task_id = harness.seed_task(instance_id="HT-M", context_status="missing")

    response = harness.post(f"/api/tasks/{task_id}/context/confirm", json=_CORRECTION)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["context_status"] == "confirmed"
    # ⚠️ 来源必须是 `manual`：确认这个动作本身就是"这四条事实由人工背书"。
    # 留成 `approval_system` 时事后分不清"系统给的就是这样"与"人看过并认可"
    assert body["context_source"] == "manual"
    assert body["our_party_name"] == "某某科技有限公司"
    assert body["our_party_business_role"] == "buyer"

    assert harness.audit_actions(task_id) == ["CONTEXT_CONFIRMED"]


def test_conflict_context_is_adjudicated_by_a_correction(harness: _Harness) -> None:
    """`conflict` 下带请求体即**人工裁定**（设计 §4.2 要求必须能裁定）。"""
    task_id = harness.seed_task(instance_id="HT-C", context_status="conflict")

    response = harness.post(
        f"/api/tasks/{task_id}/context/confirm",
        json={**_CORRECTION, "our_party_business_role": "seller"},
    )

    assert response.status_code == 200
    assert response.json()["our_party_business_role"] == "seller"


def test_correction_is_idempotent_when_nothing_changes(harness: _Harness) -> None:
    """四条都没变时**不追加审计事件**（重复动作只算一次业务事实）。"""
    task_id = harness.seed_task(instance_id="HT-I", context_status="missing")
    harness.post(f"/api/tasks/{task_id}/context/confirm", json=_CORRECTION)

    again = harness.post(f"/api/tasks/{task_id}/context/confirm", json=_CORRECTION)

    assert again.status_code == 200
    assert harness.audit_actions(task_id) == ["CONTEXT_CONFIRMED"]


def test_correction_records_which_fields_changed(harness: _Harness) -> None:
    """审计里记**真正改了哪几条**（不是"提交了哪几条"）。

    种子任务已带 `party_a` / `buyer` / `procurement`，与修正值相同 ——
    因此第一次修正**只有名称变了**。这个区分很重要：把"提交的四个字段"
    全记成"改动"时，审计里会出现三条根本没改的记录，
    而"这次人工到底动了什么"就更难回答了。
    """
    task_id = harness.seed_task(instance_id="HT-CH", context_status="missing")
    harness.post(f"/api/tasks/{task_id}/context/confirm", json=_CORRECTION)

    harness.post(
        f"/api/tasks/{task_id}/context/confirm",
        json={**_CORRECTION, "our_party_business_role": "seller"},
    )

    details = harness.audit_details(task_id)
    assert details[0]["changed_fields"] == ["our_party_name"]
    assert details[1]["changed_fields"] == ["our_party_business_role"]


def test_pure_confirmation_records_no_changed_fields(harness: _Harness) -> None:
    """纯确认（不带请求体）时 `changed_fields` 为空 —— 一条都没改。"""
    task_id = harness.seed_task(instance_id="HT-OK", context_status="complete")

    harness.post(f"/api/tasks/{task_id}/context/confirm")

    assert harness.audit_details(task_id)[0]["changed_fields"] == []


def test_correction_rejects_a_partial_submission(harness: _Harness) -> None:
    """**四条必须齐全**：只给两条时剩下两条还是旧值，会自相矛盾。"""
    task_id = harness.seed_task(instance_id="HT-P", context_status="missing")

    response = harness.post(
        f"/api/tasks/{task_id}/context/confirm",
        json={"our_party_name": "只给一条"},
    )

    assert response.status_code == 422


def test_correction_rejects_an_unknown_enum_value(harness: _Harness) -> None:
    """取值域由枚举校验：拼错的角色名必须被拒，而不是以 200 落库。

    落库后才暴露的话，规则侧会把它读成"未知角色"—— 方向敏感的规则
    全部不适用，而报告上完全看不出异常。
    """
    task_id = harness.seed_task(instance_id="HT-E", context_status="missing")

    response = harness.post(
        f"/api/tasks/{task_id}/context/confirm",
        json={**_CORRECTION, "our_party_business_role": "buyerr"},
    )

    assert response.status_code == 422
    assert harness.audit_actions(task_id) == []


def test_correction_requires_the_confirm_permission(harness: _Harness) -> None:
    """修正需要 `result:confirm`（与确认同一档权限）。"""
    task_id = harness.seed_task(instance_id="HT-R", context_status="missing")
    harness.act_as(_actor("auditor", TENANT_A, roles=[Role.READ_ONLY_AUDITOR]))

    response = harness.post(f"/api/tasks/{task_id}/context/confirm", json=_CORRECTION)

    assert response.status_code == 403
    assert harness.audit_actions(task_id) == []


def test_detail_exposes_both_sides_of_a_context_conflict(harness: _Harness) -> None:
    """`conflict` 时下发**两个来源的对照**（设计 §4.2 要求"显示冲突双方"）。

    只说"冲突了"而说不出"哪两个值冲突"时，人的下一步只能是猜 ——
    而这一屏的全部意义就是让他快速裁定。
    """
    conflict = json.dumps(
        {
            "declared": {
                "our_party_name": "示例科技有限公司",
                "our_party_business_role": "buyer",
            },
            "confirmed": {
                "our_party_name": "示例科技有限公司",
                "our_party_business_role": "seller",
            },
        }
    )
    task_id = harness.seed_task(
        instance_id="HT-CF",
        context_status="conflict",
        context_conflict_json=conflict,
    )

    body = harness.get(f"/api/tasks/{task_id}").json()

    assert body["context_status"] == "conflict"
    assert body["context_conflict"]["declared"]["our_party_business_role"] == "buyer"
    assert body["context_conflict"]["confirmed"]["our_party_business_role"] == "seller"


def test_context_conflict_is_none_when_there_is_no_conflict(
    harness: _Harness,
) -> None:
    """没有冲突时是 `None`，而不是一个空对象。

    空对象读起来像"冲突，但没有细节"，而真相是"没有冲突" ——
    界面据此渲染的提示会完全相反。
    """
    task_id = harness.seed_task(instance_id="HT-OK", context_status="complete")

    body = harness.get(f"/api/tasks/{task_id}").json()

    assert body["context_conflict"] is None


def test_correction_resolves_a_conflict_and_clears_the_record(
    harness: _Harness,
) -> None:
    """人工裁定即冲突解决：回到 `confirmed` **且清掉对照记录**。

    ⚠️ 留着记录会让下一个人以为冲突还在 ——
    "状态说没事、旁边挂着一份冲突对照"是最难解释的一种组合。
    """
    task_id = harness.seed_task(
        instance_id="HT-CF",
        context_status="conflict",
        context_conflict_json=json.dumps({"declared": {}, "confirmed": {}}),
    )

    response = harness.post(f"/api/tasks/{task_id}/context/confirm", json=_CORRECTION)

    assert response.status_code == 200
    assert response.json()["context_status"] == "confirmed"
    assert harness.get(f"/api/tasks/{task_id}").json()["context_conflict"] is None


def test_attachment_count_agrees_between_list_and_detail(harness: _Harness) -> None:
    """附件数在列表与详情里是**同一个数**。

    ⚠️ 这条守的是一次实现合流：列表用批量 `GROUP BY`、详情原本用单条
    `COUNT(*)`，两处各写一遍时，"列表说 1 个、详情说 2 个"这种不一致
    没人查得出来（两边都在自己的页面上"看起来对"）。
    """
    with_attachment = harness.seed_task(instance_id="HT-HAS")
    harness.seed_parse(with_attachment)
    without = harness.seed_task(instance_id="HT-NONE")

    rows = {
        item["task_id"]: item["attachment_count"]
        for item in harness.get("/api/tasks").json()["items"]
    }

    assert rows[with_attachment] == 1
    assert rows[without] == 0
    assert harness.get(f"/api/tasks/{with_attachment}").json()["attachment_count"] == 1
    assert harness.get(f"/api/tasks/{without}").json()["attachment_count"] == 0


def test_risk_levels_do_not_leak_between_tasks(harness: _Harness) -> None:
    """整页一次取回时，每条任务只拿到**自己**的结果。

    批量查询写错时（例如把 `rank == 1` 的分区键写成常量），
    结果是"整页都显示同一条任务的风险等级"—— 而它在只有一条任务时完全正常。
    """
    risky = harness.seed_task(instance_id="HT-RISKY")
    parse_risky = harness.seed_parse(risky)
    run_risky = harness.seed_run(risky, parse_risky)
    harness.seed_result(task_id=risky, run_id=run_risky, risk_level="high")

    plain = harness.seed_task(instance_id="HT-PLAIN")

    items = {
        item["task_id"]: item["overall_risk_level"]
        for item in harness.get("/api/tasks").json()["items"]
    }

    assert items[risky] == "high"
    assert items[plain] is None
