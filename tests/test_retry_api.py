"""人工重试、日志与审计接口（M7 / Task 5）。

## 本文件守住的是"看起来做了、其实没做"这一类

1. **重试矩阵**：`blocked_stage` 决定**重跑哪一步**。搞错的后果不是报错，
   而是"任务回到了 `parsing`，然后永远停在那里"——作业排了，但没有任何
   Worker 会领取它。
2. **回写只重回写**：回写失败若顺手重排一个解析作业，会把一次送达失败
   放大成一次重新解析 + 重新审查。
3. **操作原因必填**：审计账只记了"有人点了重试"时，没人回答得了
   "当时为什么要重试"——而"抖动后重试"与"排查后重试"在事后是两件事。
4. **失败也要留痕**：`transactional_session` 对业务异常**也提交**，
   因此"先改一半再报错"会在库里留下"报错了但数据已改"的记录。
   本文件的拒绝用例同时断言**目标字段没有被改动**。
5. **权限**：重试是运维动作（`ops:retry`），不是"任何写权限"。
   法务审核人与只读审计都应被拒 —— 只断言只读被拒时，
   一个把 `review:execute` 当通行证的实现照样通过。
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import get_actor, get_db
from app.auth import Actor, Role
from app.config import PROJECT_ROOT
from app.db import transactional_session
from app.enums import (
    AuditAction,
    JobStatus,
    JobType,
    OutboxStatus,
    TaskStatus,
    WriteStatus,
)
from app.main import app
from app.models import (
    ApprovalAttachment,
    ApprovalTask,
    AuditEvent,
    CommentLog,
    ContractParse,
    OutboxEvent,
    ReviewResult,
    ReviewRun,
    TaskLog,
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


#: 系统管理员：唯一拥有 `ops:retry` / `audit:read` 的角色。
_ADMIN = _actor("admin-a", TENANT_A)
#: 法务审核人：有写权限（保存/确认/回写），但**没有**运维重试权限。
_REVIEWER = _actor("reviewer-1", TENANT_A, roles=[Role.LEGAL_REVIEWER])
_AUDITOR = _actor("auditor-1", TENANT_A, roles=[Role.READ_ONLY_AUDITOR])


# ============================================================
# 测试台
# ============================================================


class _Harness:
    def __init__(self, work_dir: Path) -> None:
        path = work_dir / "retry.db"
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
        self.current = {"actor": _ADMIN}

    def install(self) -> None:
        def session_dependency():
            yield from transactional_session(self.factory())

        app.dependency_overrides[get_db] = session_dependency
        app.dependency_overrides[get_actor] = lambda: self.current["actor"]

    def uninstall(self) -> None:
        app.dependency_overrides.clear()
        self.engine.dispose()

    def act_as(self, actor: Actor) -> None:
        self.current["actor"] = actor

    def session(self) -> Session:
        return self.factory()

    # --- 请求 ---

    def retry(self, task_id: int, reason: str = "网络抖动已排除，人工重试"):
        return self.client.post(f"/api/tasks/{task_id}/retry", json={"reason": reason})

    def get(self, url: str):
        return self.client.get(url)

    # --- 种子 ---

    def seed_task(
        self,
        *,
        instance_id: str,
        tenant_id: str = TENANT_A,
        task_status: str = TaskStatus.BLOCKED.value,
        blocked_stage: str | None = JobType.PARSE.value,
        write_status: str = WriteStatus.NOT_WRITTEN.value,
    ) -> int:
        with self.session() as session:
            task = ApprovalTask(
                provider="mock",
                tenant_id=tenant_id,
                instance_id=instance_id,
                approval_code=instance_id,
                approval_title=f"{instance_id} 合同",
                task_status=task_status,
                blocked_stage=blocked_stage,
                last_error_code="PDF_CORRUPT",
                block_reason="PDF 损坏",
                write_status=write_status,
                context_status="complete",
            )
            session.add(task)
            session.commit()
            return task.id

    def seed_job(
        self,
        task_id: int,
        *,
        job_type: str,
        job_status: str = JobStatus.FAILED.value,
        attempt_no: int = 3,
        max_attempts: int = 3,
    ) -> int:
        with self.session() as session:
            job = WorkflowJob(
                task_id=task_id,
                job_type=job_type,
                job_status=job_status,
                attempt_no=attempt_no,
                max_attempts=max_attempts,
                idempotency_key=f"job-{task_id}-{job_type}",
                input_json='{"parse_id": 1}',
                input_digest=hashlib.sha256(b'{"parse_id": 1}').hexdigest(),
                last_error_code="PDF_CORRUPT",
                last_error_text="PDF 损坏",
            )
            session.add(job)
            session.commit()
            return job.id

    def seed_result_chain(self, task_id: int) -> int:
        """建 附件 → 解析 → 批次 → 结果，返回 `result_id`。"""
        with self.session() as session:
            attachment = ApprovalAttachment(
                task_id=task_id,
                attachment_id=f"A-{task_id}",
                file_name="contract.pdf",
                content_type="application/pdf",
                download_status="success",
                object_key=f"sha256/{task_id}.pdf",
                file_checksum="a" * 64,
            )
            session.add(attachment)
            session.flush()
            parse = ContractParse(
                task_id=task_id,
                attachment_id=attachment.id,
                parse_status="succeeded",
                parse_version=1,
            )
            session.add(parse)
            session.flush()
            run = ReviewRun(
                task_id=task_id, parse_id=parse.id, version_no=1, run_status="completed"
            )
            session.add(run)
            session.flush()
            result = ReviewResult(
                task_id=task_id,
                run_id=run.id,
                overall_risk_level="low",
                summary_text="摘要",
                focus_points_json="[]",
                comment_text="正文",
                review_status="complete",
                version_no=1,
                result_fingerprint=f"fp-{task_id}",
                content_digest="b" * 64,
            )
            session.add(result)
            session.commit()
            return result.id

    def seed_failed_writeback(self, task_id: int, result_id: int) -> tuple[int, int]:
        """建一次**失败的回写尝试** + 一条**已耗尽的 Outbox 事件**。"""
        key = f"wb-{task_id}-{result_id}"
        with self.session() as session:
            attempt = CommentLog(
                task_id=task_id,
                review_id=result_id,
                write_status=WriteStatus.FAILED.value,
                reason_code="APPROVAL_API_ERROR",
                reason_text="对端 500",
                content_digest="b" * 64,
                idempotency_key=key,
                attempt_no=5,
                operator_name="张三",
            )
            session.add(attempt)
            session.flush()
            event = OutboxEvent(
                aggregate_type="comment_log",
                aggregate_id=attempt.id,
                event_type="WRITE_APPROVAL_COMMENT",
                payload_json="{}",
                idempotency_key=key,
                event_status=OutboxStatus.FAILED.value,
                attempt_no=5,
                max_attempts=5,
                last_error_code="APPROVAL_API_ERROR",
                last_error_text="对端 500",
            )
            session.add(event)
            session.commit()
            return attempt.id, event.id

    # --- 读回 ---

    def jobs_of(self, task_id: int, job_type: str) -> list[WorkflowJob]:
        with self.session() as session:
            return list(
                session.execute(
                    select(WorkflowJob).where(
                        WorkflowJob.task_id == task_id,
                        WorkflowJob.job_type == job_type,
                    )
                ).scalars()
            )

    def job_count(self, task_id: int) -> int:
        with self.session() as session:
            return len(
                list(
                    session.execute(
                        select(WorkflowJob.id).where(WorkflowJob.task_id == task_id)
                    ).scalars()
                )
            )

    def task_row(self, task_id: int) -> ApprovalTask:
        with self.session() as session:
            row = session.get(ApprovalTask, task_id)
            assert row is not None
            session.expunge(row)
            return row

    def audit_rows(self, task_id: int) -> list[AuditEvent]:
        with self.session() as session:
            return list(
                session.execute(
                    select(AuditEvent).where(AuditEvent.task_id == task_id)
                ).scalars()
            )

    def log_text(self, task_id: int) -> str:
        with self.session() as session:
            rows = session.execute(
                select(TaskLog.log_content).where(TaskLog.task_id == task_id)
            ).scalars()
            return "\n".join(rows)


@pytest.fixture()
def harness(work_dir: Path):
    built = _Harness(work_dir)
    built.install()
    try:
        yield built
    finally:
        built.uninstall()


# ============================================================
# 1. 重试矩阵
# ============================================================


@pytest.mark.parametrize(
    ("blocked_stage", "expected_job_type", "expected_status"),
    [
        # 解析失败 → 重跑解析；任务回 parsing
        (JobType.PARSE.value, JobType.PARSE.value, TaskStatus.PARSING.value),
        # 规则失败 → 重跑规则；任务回 reviewing
        (JobType.RULE.value, JobType.RULE.value, TaskStatus.REVIEWING.value),
        # 结果失败 → **重跑保存**（不是重跑规则）；任务回 reviewing
        (JobType.RESULT.value, JobType.RESULT.value, TaskStatus.REVIEWING.value),
    ],
    ids=["parse", "rule", "result"],
)
def test_retry_matrix_reruns_exactly_the_failed_step(
    harness: _Harness, blocked_stage: str, expected_job_type: str, expected_status: str
) -> None:
    """**验收**：失败位置 → 重跑哪一步 → 任务回到哪个状态。

    ⚠️ 只断言状态、不断言作业时，一个"把任务状态改回去但没排任何作业"的
    实现照样通过 —— 而那正是最坏的一种：界面显示"已重试"，
    Worker 那边什么都没发生，任务永远停在原地。
    """
    task_id = harness.seed_task(instance_id="HT-1", blocked_stage=blocked_stage)
    harness.seed_job(task_id, job_type=blocked_stage, job_status=JobStatus.FAILED.value)

    response = harness.retry(task_id)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["resumed_status"] == expected_status
    assert body["action"] == "job_queued"
    assert body["job_type"] == expected_job_type

    jobs = harness.jobs_of(task_id, expected_job_type)
    assert len(jobs) == 1, "重试沿用**同一个**作业（同一份冻结输入），不新建"
    assert jobs[0].job_status == JobStatus.QUEUED.value
    assert jobs[0].attempt_no == 0, "显式重试应拿回**完整**的重试预算"
    assert jobs[0].last_error_code is None
    assert harness.job_count(task_id) == 1, "重试不得凭空多出别类型的作业"

    task = harness.task_row(task_id)
    assert task.task_status == expected_status
    assert task.blocked_stage is None, "恢复后不再是阻塞状态，三个阻塞字段都要清"
    assert task.last_error_code is None
    assert task.block_reason is None
    assert task.retry_count == 1


def test_writeback_retry_rearms_delivery_and_does_not_rerun_anything(
    harness: _Harness,
) -> None:
    """**验收**：回写失败**只**重新武装投递 —— 不重排解析、不重跑规则。

    回写意图早在 `comment_logs` + `outbox_events` 里，失败的是**送达**。
    再登记一次意图没有意义（幂等键相同 → 复用旧尝试），
    而顺手重排一个解析作业会把"一次送达失败"放大成"重新解析 + 重新审查"。
    """
    task_id = harness.seed_task(
        instance_id="HT-1",
        blocked_stage=JobType.WRITEBACK.value,
        write_status=WriteStatus.FAILED.value,
    )
    result_id = harness.seed_result_chain(task_id)
    attempt_id, event_id = harness.seed_failed_writeback(task_id, result_id)

    response = harness.retry(task_id)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["action"] == "writeback_rearmed"
    assert body["outbox_event_id"] == event_id
    assert body["attempt_id"] == attempt_id
    assert body["job_id"] is None
    assert body["resumed_status"] == TaskStatus.REVIEWING.value

    with harness.session() as session:
        event = session.get(OutboxEvent, event_id)
        assert event is not None
        assert event.event_status == OutboxStatus.PENDING.value
        assert event.attempt_no == 0, "不归还预算，重武装出来的事件会立刻再次耗尽"
        assert event.next_retry_at is None
        assert event.lease_owner is None
        assert event.last_error_code is None
        assert event.delivered_at is None

        attempt = session.get(CommentLog, attempt_id)
        assert attempt is not None
        assert attempt.write_status == WriteStatus.WRITING.value
        assert attempt.reason_code is None

    assert harness.job_count(task_id) == 0, "回写重试**不得**产生任何新作业"
    assert harness.task_row(task_id).write_status == WriteStatus.WRITING.value


def test_unresumable_stage_is_rejected_instead_of_queuing_a_dead_job(
    harness: _Harness,
) -> None:
    """**验收**：`download` 失败 → 409 + **可执行的指引**，不是排一个死作业。

    ⚠️ 下载由工具 3 在**同步**路径上完成，Worker 不领取 `download` 作业。
    若这里"顺手"排一个 download 作业，任务会回到 `parsing` 然后**永远停住** ——
    而响应显示 200「已重试」。恢复入口确实存在（重跑工具 3 成功后
    `attachment_service` 会自己把任务从检查点拉回来），把它写出来才是完整的回答。
    """
    task_id = harness.seed_task(
        instance_id="HT-1", blocked_stage=JobType.DOWNLOAD.value
    )
    harness.seed_job(task_id, job_type=JobType.DOWNLOAD.value)

    response = harness.retry(task_id)

    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "RETRY_NOT_SUPPORTED"
    assert "工具 3" in response.text, "拒绝必须告诉操作员**该去点哪个按钮**"

    # 拒绝是**在读之前**发生的：任务状态与作业一个字段都不许动
    task = harness.task_row(task_id)
    assert task.task_status == TaskStatus.BLOCKED.value
    assert task.blocked_stage == JobType.DOWNLOAD.value
    assert task.retry_count == 0
    jobs = harness.jobs_of(task_id, JobType.DOWNLOAD.value)
    assert jobs[0].job_status == JobStatus.FAILED.value
    assert jobs[0].attempt_no == 3, "被拒的重试不该消耗任何东西"


# ============================================================
# 2. 前置条件与拒绝
# ============================================================


def test_retry_requires_an_operator_reason(harness: _Harness) -> None:
    """**验收**：操作原因必填 —— 缺字段与纯空白得到**同一个** 400。

    ⚠️ 若把 `min_length=1` 写进请求模型，缺字段会得到框架的 422 而空白串得到 400：
    同一件事两种状态码，而"原因必填"是一条**业务规则**（它要进审计账），
    因此只在服务层判一次。
    """
    task_id = harness.seed_task(instance_id="HT-1")

    missing = harness.client.post(f"/api/tasks/{task_id}/retry", json={})
    blank = harness.retry(task_id, reason="   ")

    assert missing.status_code == 400, missing.text
    assert blank.status_code == 400, blank.text
    assert missing.json()["error_code"] == blank.json()["error_code"] == "INVALID_ARGUMENT"
    assert "操作原因" in missing.text
    assert harness.audit_rows(task_id) == [], "被拒的重试不产生审计事件"


def test_only_blocked_tasks_can_be_retried(harness: _Harness) -> None:
    """非阻塞任务没有"要恢复的失败位置"：409，而不是假装重试成功。"""
    task_id = harness.seed_task(
        instance_id="HT-1",
        task_status=TaskStatus.REVIEWING.value,
        blocked_stage=None,
    )

    response = harness.retry(task_id)

    assert response.status_code == 409
    assert response.json()["error_code"] == "INVALID_STATE_TRANSITION"
    assert harness.task_row(task_id).retry_count == 0


def test_a_running_job_is_not_reset(harness: _Harness) -> None:
    """仍在执行中的作业不能被重排：它正持有租约，重排会让两边互相覆盖。"""
    task_id = harness.seed_task(instance_id="HT-1")
    harness.seed_job(
        task_id, job_type=JobType.PARSE.value, job_status=JobStatus.RUNNING.value
    )

    response = harness.retry(task_id)

    assert response.status_code == 409
    assert response.json()["error_code"] == "INVALID_STATE_TRANSITION"
    assert harness.task_row(task_id).task_status == TaskStatus.BLOCKED.value


def test_retry_without_a_matching_job_is_rejected(harness: _Harness) -> None:
    """没有可沿用的冻结输入时不硬造一个作业出来：409 + 说明。"""
    task_id = harness.seed_task(instance_id="HT-1", blocked_stage=JobType.PARSE.value)

    response = harness.retry(task_id)

    assert response.status_code == 409
    assert response.json()["error_code"] == "RETRY_NOT_SUPPORTED"
    assert harness.task_row(task_id).task_status == TaskStatus.BLOCKED.value


# ============================================================
# 3. 审计与日志
# ============================================================


def test_retry_is_audited_with_operator_and_reason(harness: _Harness) -> None:
    """**验收**：审计账要能回答"谁、什么时候、为什么重试的"。"""
    task_id = harness.seed_task(instance_id="HT-1")
    harness.seed_job(task_id, job_type=JobType.PARSE.value)

    harness.retry(task_id, reason="对端已恢复，重试解析")

    events = harness.audit_rows(task_id)
    assert [event.action for event in events] == [AuditAction.TASK_RETRIED.value]
    event = events[0]
    assert event.actor_id == "admin-a"
    assert event.actor_name == "admin-a"
    assert event.target_type == "approval_task"
    assert event.target_id == task_id
    assert "对端已恢复，重试解析" in (event.detail_json or "")
    assert "parse" in (event.detail_json or "")

    assert "对端已恢复，重试解析" in harness.log_text(task_id)


def test_logs_endpoint_returns_the_retry_trail(harness: _Harness) -> None:
    """日志接口按关联 ID 可过滤 —— 那是跨进程追踪的唯一线索。"""
    task_id = harness.seed_task(instance_id="HT-1")
    harness.seed_job(task_id, job_type=JobType.PARSE.value)
    harness.retry(task_id)

    body = harness.get(f"/api/logs/{task_id}").json()
    assert body["total"] == 1
    row = body["items"][0]
    assert row["log_type"] == "system"
    assert "人工重试" in row["log_content"]

    # 白名单校验：拼错的过滤值得到 400，而不是空列表
    assert harness.get(f"/api/logs/{task_id}?log_level=nonsense").status_code == 400
    assert harness.get(f"/api/logs/{task_id}?correlation_id=corr-x").json()["total"] == 0


def test_audit_endpoint_hides_system_events_by_default(harness: _Harness) -> None:
    """**验收**：全局（不属于任何任务）的审计事件默认不可见，需显式打开。

    ⚠️ 默认带上它们时，每个租户都能读到全局配置变更 —— 多租户下是越权。
    默认值选「看不到」是 fail-closed 的方向。
    """
    task_id = harness.seed_task(instance_id="HT-1")
    harness.seed_job(task_id, job_type=JobType.PARSE.value)
    harness.retry(task_id)

    with harness.session() as session:
        session.add(
            AuditEvent(
                task_id=None,  # 规则变更：不属于任何任务
                actor_id="admin-a",
                actor_name="admin-a",
                action=AuditAction.RULE_UPDATED.value,
                target_type="review_rule",
                target_id=1,
                detail_json='{"changed_fields": ["match_text"]}',
            )
        )
        session.commit()

    default = harness.get("/api/audit").json()
    assert default["total"] == 1
    assert default["items"][0]["action"] == AuditAction.TASK_RETRIED.value

    with_system = harness.get("/api/audit?include_system=true").json()
    assert with_system["total"] == 2
    assert {item["action"] for item in with_system["items"]} == {
        AuditAction.TASK_RETRIED.value,
        AuditAction.RULE_UPDATED.value,
    }

    # 白名单校验：拼错的 action 得到 400
    assert harness.get("/api/audit?action=NOT_AN_ACTION").status_code == 400


# ============================================================
# 4. 权限与租户
# ============================================================


def test_retry_requires_ops_permission(harness: _Harness) -> None:
    """**验收**：重试是运维动作 —— 只读审计**与法务审核人**都应被拒。

    只断言"只读被拒"时，一个把任意写权限当通行证的实现照样通过。
    """
    task_id = harness.seed_task(instance_id="HT-1")
    harness.seed_job(task_id, job_type=JobType.PARSE.value)

    for actor in (_AUDITOR, _REVIEWER):
        harness.act_as(actor)
        response = harness.retry(task_id)
        assert response.status_code == 403, f"{actor.actor_id} 不应能重试"
        assert response.json()["error_code"] == "PERMISSION_DENIED"

    assert harness.task_row(task_id).task_status == TaskStatus.BLOCKED.value
    assert harness.audit_rows(task_id) == []


def test_only_audit_readers_can_read_the_audit_trail(harness: _Harness) -> None:
    """审计账只有管理员与审计员能读。"""
    harness.act_as(_REVIEWER)
    assert harness.get("/api/audit").status_code == 403

    harness.act_as(_AUDITOR)
    assert harness.get("/api/audit").status_code == 200


def test_another_tenant_cannot_retry_a_task_by_guessing_its_id(
    harness: _Harness,
) -> None:
    """跨租户直接猜 id → 404，且**目标任务一个字段都没变**（含作业与审计）。"""
    task_id = harness.seed_task(instance_id="HT-A", tenant_id=TENANT_A)
    harness.seed_job(task_id, job_type=JobType.PARSE.value)

    harness.act_as(_actor("admin-b", TENANT_B, roles=[Role.SYSTEM_ADMIN]))
    response = harness.retry(task_id)

    assert response.status_code == 404
    assert "HT-A" not in response.text

    harness.act_as(_ADMIN)
    task = harness.task_row(task_id)
    assert task.retry_count == 0
    assert task.blocked_stage == JobType.PARSE.value
    assert harness.audit_rows(task_id) == []
    assert harness.jobs_of(task_id, JobType.PARSE.value)[0].job_status == (
        JobStatus.FAILED.value
    )


def test_logs_and_audit_stay_inside_the_tenant(harness: _Harness) -> None:
    """日志按任务归属过租户门；审计列表不会混进别家的任务事件。"""
    task_id = harness.seed_task(instance_id="HT-A", tenant_id=TENANT_A)
    other = harness.seed_task(instance_id="HT-B", tenant_id=TENANT_B)
    harness.seed_job(other, job_type=JobType.PARSE.value)

    harness.act_as(_actor("admin-b", TENANT_B, roles=[Role.SYSTEM_ADMIN]))
    assert harness.get(f"/api/logs/{task_id}").status_code == 404
    assert harness.retry(task_id).status_code == 404

    # 租户 B 看得到自己的事件（先让它产生一条），看不到 A 的
    harness.retry(other)
    body = harness.get("/api/audit").json()
    assert body["total"] == 1
    assert body["items"][0]["task_id"] == other
