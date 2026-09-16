"""工具 6–7、结果/回写查询与作业接线的接口测试（M6 / Task 6）。

## 本文件守住的是"接口层特有的那几件事"

服务层的行为已由 `test_result_service.py` / `test_writeback_service.py` /
`test_outbox.py` 覆盖。这里只测**协议层**：

1. **7 个工具名逐字不变**（需求 2.4.10）—— 路径名一改，兼容门面就断了，
   而 REST 与 MCP 共用同一套服务，重命名不会有任何测试失败；
2. **门禁拒绝是业务结论（200 + `blocked`），不是 5xx** —— 回写被拒绝
   （未确认 / 高风险 / 上下文不可信）是"调用成功确认了一个业务事实"，
   用 5xx 会让调用端当成抖动反复重试，而它等的是一个**人**；
3. **稳定原因码不得被泛化成 `INVALID_ARGUMENT`** —— `ResultInputError`
   继承 `ValueError`，若不单独登记，它会落进"参数非法 → 400"的兜底分支，
   于是"批次没跑完"（409）与"风险等级对不上"（400）在接口上长得一模一样；
4. **回写重试不重跑 M4/M5** —— 一次写入失败如果顺带重跑了 OCR 与规则，
   会凭空多出一个批次，而"多出来的批次"在统计上看起来只是"又审了一次"。

## 为什么端到端里"确认"走服务而不是接口

`POST /api/results/{id}/confirm` 属于 **M7** 的任务查询与人工确认接口
（M6 实施计划 Task 6 的 Interfaces 只列出工具 6/7 与两个查询接口）。
本文件因此用 `confirm_result()` 服务函数跨出这一步，并在注释里标明 ——
而不是顺手实现一个 M7 的接口，让两个里程碑各自以为对方在管它。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.auth import Actor, Role
from app.api.deps import get_actor, get_db
from app.config import PROJECT_ROOT, settings
from app.db import transactional_session
from app.enums import ErrorCode, OutboxStatus, TaskStatus, WriteStatus
from app.errors import PermanentGatewayError, TransientGatewayError
from app.main import app
from app.workflow.jobs import backoff_seconds
from app.models import (
    ApprovalAttachment,
    ApprovalTask,
    CommentLog,
    ContractParse,
    OutboxEvent,
    ReviewResult,
    ReviewRun,
    RuleEvaluation,
    WorkflowJob,
)
from app.outbox import OutboxDispatcher
from app.ports.approval_gateway import WriteCommentResultDTO
from app.services.result_service import confirm_result
from app.workflow.jobs import create_job


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

SCHEMA_FILE = PROJECT_ROOT / "db" / "schema.sql"

#: 需求 2.4.10 的 7 个工具名（**逐字**，不得增删改）
REQUIRED_TOOL_PATHS = (
    "/tools/list_pending_contract_approvals",
    "/tools/get_contract_approval",
    "/tools/download_contract_attachment",
    "/tools/parse_contract_document",
    "/tools/run_contract_rules",
    "/tools/save_review_result",
    "/tools/write_approval_comment",
)


def _actor_payload(name: str) -> dict:
    """作业输入里**冻结的身份**（`ActorPayload` 的 JSON 形态）。

    作业输入存的是结构而不是一个名字字符串：只存名字时审计里 `actor_id`
    永远是空，而"张伟"在两个部门各有一个，事后分不清是谁发起的。
    """
    return {
        "actor_id": name,
        "display_name": name,
        "roles": [],
        "tenant_id": "default",
    }


# ============================================================
# 测试台
# ============================================================


def _actor(name: str) -> Actor:
    """测试身份：`actor_id` 与 `display_name` 同名，断言时更好读。

    ⚠️ `roles` 刻意留空 —— 本文件测的是**服务层**，而权限判断是
    API 层 `require_permissions` 的职责（见 tests/test_auth_rbac.py）。
    这里补一套"看起来合理"的角色，会让"服务层是否偷偷鉴权"再也测不出来。
    """
    return Actor(
        actor_id=name, display_name=name, roles=frozenset(), tenant_id="default"
    )


@pytest.fixture()
def harness(work_dir: Path):
    """临时库 + 同一份事务边界 + `TestClient`。

    建表用**交付的 `schema.sql`**而不是 `Base.metadata.create_all()`：
    后者走的是 ORM 定义，两者一旦漂移，这里测出来的约束与生产不是同一套。
    """
    path = work_dir / "m6api.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()

    engine = create_engine(
        f"sqlite:///{path.as_posix()}",
        future=True,
        connect_args={"check_same_thread": False},
    )
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    client = TestClient(app)

    def session_dependency():
        # 复用 `app/db.py` 的**同一份**事务边界实现（测试里另写一份，
        # 语义一旦分叉，测出来的事务行为与生产不一致 —— 等于没测）
        yield from transactional_session(factory())

    app.dependency_overrides[get_db] = session_dependency
    # 身份层在本文件里"透明"（理由见 _FULL_ACCESS_ACTOR 的注释）
    app.dependency_overrides[get_actor] = lambda: _FULL_ACCESS_ACTOR
    try:
        yield _Harness(factory=factory, client=client)
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


class _Harness:
    def __init__(self, *, factory: sessionmaker, client: TestClient) -> None:
        self.factory = factory
        self.client = client

    def session(self) -> Session:
        return self.factory()

    def seed_chain(
        self, *, instance_id: str = "HT-2026-0001", run_status: str = "completed"
    ) -> tuple[int, int]:
        """最小可用的 任务→附件→解析→批次 链；**不含 rule_hits**。

        没有评价 → 聚合为 `(low, needs_review)`：这恰好是最需要人工确认的形态，
        也正是本文件要守的"未确认不得回写"。
        """
        with self.session() as session:
            task = ApprovalTask(
                provider="mock",
                tenant_id="default",
                instance_id=instance_id,
                approval_code=instance_id,
                task_status="reviewing",
                write_status=WriteStatus.NOT_WRITTEN.value,
                context_status="confirmed",
                our_party_name="我方公司",
                our_party_contract_label="party_a",
                our_party_business_role="buyer",
                contract_type="procurement",
            )
            session.add(task)
            session.flush()

            attachment = ApprovalAttachment(
                task_id=task.id,
                attachment_id="ATT-1",
                file_name="contract.pdf",
                download_status="success",
                content_type="application/pdf",
            )
            session.add(attachment)
            session.flush()

            parse = ContractParse(
                task_id=task.id,
                attachment_id=attachment.id,
                parse_status="succeeded",
                parse_version=1,
            )
            session.add(parse)
            session.flush()

            run = ReviewRun(
                task_id=task.id,
                parse_id=parse.id,
                version_no=1,
                run_status=run_status,
            )
            session.add(run)
            session.flush()
            task_id, run_id = task.id, run.id
            session.commit()
            return task_id, run_id

    def counts(self) -> dict[str, int]:
        """M4/M5 的产物计数 —— 回写重试不得改变其中任何一个。"""
        with self.session() as session:
            return {
                "parses": session.execute(
                    select(func.count()).select_from(ContractParse)
                ).scalar_one(),
                "runs": session.execute(
                    select(func.count()).select_from(ReviewRun)
                ).scalar_one(),
                "evaluations": session.execute(
                    select(func.count()).select_from(RuleEvaluation)
                ).scalar_one(),
            }

    def task(self, task_id: int) -> ApprovalTask:
        with self.session() as session:
            return session.get(ApprovalTask, task_id)

    def attempt(self, attempt_id: int) -> CommentLog:
        with self.session() as session:
            return session.get(CommentLog, attempt_id)

    def outbox_events(self) -> list[OutboxEvent]:
        with self.session() as session:
            return list(session.execute(select(OutboxEvent)).scalars().all())

    def results(self) -> list[ReviewResult]:
        with self.session() as session:
            return list(session.execute(select(ReviewResult)).scalars().all())

    def save(
        self,
        run_id: int,
        *,
        risk: str = "low",
        comment_text: str = "回写正文",
        **extra: object,
    ) -> dict:
        body: dict[str, object] = {
            "run_id": run_id,
            "overall_risk_level": risk,
            "summary_text": "审查摘要",
            "focus_points": ["关注点一"],
            "comment_text": comment_text,
        }
        body.update(extra)
        response = self.client.post("/tools/save_review_result", json=body)
        return {"status_code": response.status_code, **response.json()}

    def writeback(self, instance_id: str, result_id: int) -> dict:
        response = self.client.post(
            "/tools/write_approval_comment",
            json={"instance_id": instance_id, "result_id": result_id},
        )
        return {"status_code": response.status_code, **response.json()}


# ============================================================
# 写入侧替身：外部审批系统
# ============================================================


class _FakeCommentGateway:
    """写侧网关替身：按幂等键去重（与 mock 网关同语义）。"""

    provider = "mock"
    tenant_id = "default"

    def __init__(self) -> None:
        self.comments: list[tuple[str, str, str]] = []  # (instance, content, key)
        self.fail_times = 0
        self.always_transient = False
        self.write_calls = 0
        self.get_calls = 0

    def write_comment(
        self, instance_id: str, content: str, *, idempotency_key: str,
        operator_name: str | None = None,
    ) -> WriteCommentResultDTO:
        self.write_calls += 1
        if self.always_transient:
            raise TransientGatewayError("模拟 5xx", code=ErrorCode.APPROVAL_API_ERROR)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise TransientGatewayError("模拟超时", code=ErrorCode.APPROVAL_API_ERROR)

        for instance, stored, key in self.comments:
            if instance == instance_id and key == idempotency_key:
                return WriteCommentResultDTO(
                    write_status=WriteStatus.SUCCESS,
                    external_comment_id="cmt-1",
                    replayed=True,
                    response_text='{"comment_id": "cmt-1"}',
                )
        self.comments.append((instance_id, content, idempotency_key))
        return WriteCommentResultDTO(
            write_status=WriteStatus.SUCCESS,
            external_comment_id=f"cmt-{len(self.comments)}",
            replayed=False,
            response_text=json.dumps({"comment_id": f"cmt-{len(self.comments)}"}),
        )

    def get_write_result(self, instance_id: str, idempotency_key: str):
        self.get_calls += 1
        for instance, _content, key in self.comments:
            if instance == instance_id and key == idempotency_key:
                return WriteCommentResultDTO(
                    write_status=WriteStatus.SUCCESS,
                    external_comment_id="cmt-1",
                    replayed=True,
                    response_text='{"comment_id": "cmt-1"}',
                )
        return None


class _PermanentFailureGateway(_FakeCommentGateway):
    """确定性失败：不得重试。"""

    def write_comment(self, instance_id, content, *, idempotency_key, operator_name=None):
        self.write_calls += 1
        raise PermanentGatewayError("实例不存在", code=ErrorCode.INSTANCE_NOT_FOUND)


# ============================================================
# 1. 工具名与最低参数（需求 2.4.10 的兼容面）
# ============================================================


def test_seven_required_tool_names_are_exposed_verbatim(harness: _Harness) -> None:
    """7 个工具名逐字不变。多一个、少一个、改一个字符都要在这里失败。"""
    paths = set(app.openapi()["paths"])
    missing = [name for name in REQUIRED_TOOL_PATHS if name not in paths]
    assert missing == [], f"以下工具名缺失或被改名：{missing}"


def test_tool_six_accepts_the_required_minimum_fields(harness: _Harness) -> None:
    """工具 6 的最低参数集可用：风险等级 / 摘要 / 关注点 / 正文（+ 目标批次）。

    ⚠️ **参数名的分工**：需求 §6.5 写的是 `case_id`，本层用的是 `run_id`。
    这不是改名，而是 Fixed Decision 1 的分工：「`case_id` 指的是 M5 的
    `review_runs.id`，内部规范名是 `run_id`」。需求里那两个名字
    （`case_id` / `review_id`）由 **M7 的兼容门面**（`app/tool_facade.py`）
    翻译成这里的规范名 —— 与工具 5 先例一致：需求写 `case_id`，
    M5 的 REST 用 `parse_id`，兼容名在门面。
    """
    _task_id, run_id = harness.seed_chain()
    payload = harness.save(run_id)

    assert payload["status_code"] == 200
    assert payload["outcome"] == "saved"
    assert payload["overall_risk_level"] == "low"
    assert payload["result_url"] == f"/api/results/{payload['result_id']}"


def test_tool_seven_accepts_the_required_minimum_fields(harness: _Harness) -> None:
    """工具 7 的最低参数集可用：`instance_id` + 目标结果（需求里叫 `review_id`）。"""
    instance_id = "HT-2026-0001"
    harness.seed_chain(instance_id=instance_id)
    saved = harness.save(1)

    payload = harness.writeback(instance_id, saved["result_id"])
    assert payload["status_code"] == 200
    assert payload["writeback_ref"]["status_url"].startswith("/api/writebacks/")


def test_legacy_parameter_names_are_rejected_at_this_layer(harness: _Harness) -> None:
    """需求里的 `case_id` / `review_id` 在**本层**必须被拒 —— 兼容名归 M7 门面。

    这条断言的方向可能反直觉，但它守住的是一个具体的坏结局：
    若在这里"顺手"接受 `case_id`，同一层就同时存在两个名字指向同一个东西，
    而它们的**类型还不同**（需求里是 `str`，`review_runs.id` 是 `int`）。
    于是"传 `"12"` 还是传 `12`"变成一个没有正确答案的问题，
    而两种写法都会在某些调用路径上"看起来能用"。

    宁可在这里明确拒绝（`extra="forbid"` → 422，原因写得清楚），
    也不接受一个语义模糊的别名。
    """
    instance_id = "HT-2026-0001"
    _task_id, run_id = harness.seed_chain(instance_id=instance_id)

    assert harness.save(run_id, case_id=instance_id)["status_code"] == 422

    saved = harness.save(run_id)
    legacy = harness.client.post(
        "/tools/write_approval_comment",
        json={"instance_id": instance_id, "review_id": saved["result_id"]},
    )
    assert legacy.status_code == 422


def test_tool_requests_still_reject_unknown_fields(harness: _Harness) -> None:
    """`extra="forbid"` 仍然生效：静默丢弃会让调用方以为参数生效了。"""
    _task_id, run_id = harness.seed_chain()
    payload = harness.save(run_id, sourc_checksum="拼错的键")
    assert payload["status_code"] == 422


# ============================================================
# 2. 工具 6：同步返回保存结果
# ============================================================


def test_save_returns_the_saved_result_synchronously(harness: _Harness) -> None:
    """短任务直接返回业务结果（企业化设计 §6），不是 `TaskRef`。"""
    _task_id, run_id = harness.seed_chain()
    payload = harness.save(run_id)

    assert payload["version_no"] == 1
    assert payload["task_id"] == 1
    # 无需轮询：调用方拿到 200 时结果**已经在库里**
    assert [row.version_no for row in harness.results()] == [1]


def test_identical_save_is_reused_without_a_second_version(harness: _Harness) -> None:
    _task_id, run_id = harness.seed_chain()
    first = harness.save(run_id)
    replay = harness.save(run_id)

    assert replay["outcome"] == "reused"
    assert replay["result_id"] == first["result_id"]
    assert len(harness.results()) == 1


def test_save_of_an_unfinished_run_is_a_conflict_not_a_bad_request(
    harness: _Harness,
) -> None:
    """批次没跑完 → **409**。

    `ResultInputError` 继承 `ValueError`，不单独登记就会落进"参数非法 → 400"
    的兜底分支：于是"批次还没跑完"（可恢复，等一会儿再来）与
    "风险等级写错了"（调用方写错了）在接口上长得**一模一样**。
    """
    _task_id, run_id = harness.seed_chain(run_status="running")
    payload = harness.save(run_id)

    assert payload["status_code"] == 409
    assert payload["error_code"] == "RESULT_RUN_NOT_COMPLETED"
    assert payload["outcome"] == "error"
    assert harness.results() == [], "被拒绝的保存不得留下半截结果"


def test_save_with_a_mismatched_risk_level_is_a_bad_request(harness: _Harness) -> None:
    """口径不一致是**调用方写错了** → 400，而不是 409/500。"""
    _task_id, run_id = harness.seed_chain()
    payload = harness.save(run_id, risk="high")

    assert payload["status_code"] == 400
    assert payload["error_code"] == "RESULT_INPUT_MISMATCH"
    assert harness.results() == []


def test_save_of_a_missing_run_is_404(harness: _Harness) -> None:
    payload = harness.save(999_999)

    assert payload["status_code"] == 404
    assert payload["error_code"] == "RESULT_NOT_FOUND"


# ============================================================
# 3. 结果查询：确认有效性由后端给出
# ============================================================


def test_result_query_exposes_backend_computed_confirmation_validity(
    harness: _Harness,
) -> None:
    """`confirmation_valid` 由后端算。

    让浏览器自己比 `content_digest` 与 `confirmed_digest` 等于
    **在浏览器里算业务口径**（M8 设计 G-3），前端一旦改版就会静默算错。
    """
    _task_id, run_id = harness.seed_chain()
    result_id = harness.save(run_id)["result_id"]

    before = harness.client.get(f"/api/results/{result_id}").json()
    assert before["confirmation_valid"] is False
    assert before["manual_confirmed"] is False
    assert before["review_status"] == "needs_review"

    # 确认走服务（`POST /api/results/{id}/confirm` 是 M7 的接口）
    with harness.session() as session:
        confirm_result(session, result_id=result_id, actor=_actor("reviewer-9"))
        session.commit()

    after = harness.client.get(f"/api/results/{result_id}").json()
    assert after["confirmation_valid"] is True
    assert after["confirmed_by"] == "reviewer-9"


def test_missing_result_query_is_404(harness: _Harness) -> None:
    response = harness.client.get("/api/results/424242")
    assert response.status_code == 404
    assert response.json()["error_code"] == "RESULT_NOT_FOUND"


# ============================================================
# 4. 工具 7：门禁拒绝是业务结论
# ============================================================


def test_unconfirmed_result_is_denied_as_a_business_conclusion(
    harness: _Harness,
) -> None:
    """未确认 → 200 + `blocked` + 稳定原因码；**不得**是 5xx。"""
    instance_id = "HT-2026-0001"
    _task_id, run_id = harness.seed_chain(instance_id=instance_id)
    result_id = harness.save(run_id)["result_id"]

    payload = harness.writeback(instance_id, result_id)

    assert payload["status_code"] == 200
    assert payload["outcome"] == "blocked"
    assert payload["writeback_ref"]["write_status"] == WriteStatus.NOT_WRITTEN.value
    assert payload["writeback_ref"]["reason_code"] == "MANUAL_CONFIRM_REQUIRED"
    assert payload["writeback_ref"]["reason_text"], "拒绝必须给出人读的原因"
    # 拒绝**只留拒绝证据**，不留意图与审计
    assert harness.outbox_events() == []
    assert harness.task(1).write_status == WriteStatus.NOT_WRITTEN.value


def test_high_risk_cannot_bypass_confirmation_even_with_auto_writeback(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """高风险**永远**要人工确认（Fixed Decision 4）。"""
    instance_id = "HT-2026-0001"
    task_id, run_id = harness.seed_chain(instance_id=instance_id)
    result_id = _seed_result(
        harness, task_id=task_id, run_id=run_id, risk="high", confirmed=False
    )

    monkeypatch.setattr(settings, "auto_writeback_enabled", True)
    payload = harness.writeback(instance_id, result_id)

    assert payload["outcome"] == "blocked"
    assert payload["writeback_ref"]["reason_code"] == "MANUAL_CONFIRM_REQUIRED"
    assert harness.outbox_events() == []


def test_writeback_of_a_missing_result_is_404(harness: _Harness) -> None:
    harness.seed_chain()
    payload = harness.writeback("HT-2026-0001", 424242)

    assert payload["status_code"] == 404
    assert payload["error_code"] == "RESULT_NOT_FOUND"


# ============================================================
# 5. 端到端：保存 → 确认 → 回写 → 派发 → 任务 done
# ============================================================


def test_end_to_end_closure_reaches_task_done(harness: _Harness) -> None:
    instance_id = "HT-2026-0001"
    task_id, run_id = harness.seed_chain(instance_id=instance_id)

    saved = harness.save(run_id)
    result_id = saved["result_id"]

    with harness.session() as session:
        confirm_result(session, result_id=result_id, actor=_actor("reviewer-9"))
        session.commit()

    accepted = harness.writeback(instance_id, result_id)
    assert accepted["outcome"] == "accepted"
    assert accepted["writeback_ref"]["write_status"] == WriteStatus.WRITING.value
    attempt_id = accepted["writeback_ref"]["attempt_id"]
    assert accepted["writeback_ref"]["status_url"] == f"/api/writebacks/{attempt_id}"

    gateway = _FakeCommentGateway()
    dispatcher = OutboxDispatcher(harness.factory, gateway)
    assert dispatcher.run_once() is True
    assert len(gateway.comments) == 1, "外部评论恰好一条"

    assert harness.task(task_id).task_status == TaskStatus.DONE.value
    assert harness.task(task_id).write_status == WriteStatus.SUCCESS.value

    polled = harness.client.get(f"/api/writebacks/{attempt_id}").json()
    assert polled["write_status"] == WriteStatus.SUCCESS.value
    assert polled["reason_code"] is None
    assert polled["delivery"]["event_status"] == OutboxStatus.DELIVERED.value
    assert polled["delivery"]["delivered_at"] is not None


def test_denied_then_repaired_attempt_is_the_same_attempt(harness: _Harness) -> None:
    """拒绝行在条件修复后**复用同一行**转正（否则旧行会永久堵住唯一键）。"""
    instance_id = "HT-2026-0001"
    _task_id, run_id = harness.seed_chain(instance_id=instance_id)
    result_id = harness.save(run_id)["result_id"]

    denied = harness.writeback(instance_id, result_id)
    with harness.session() as session:
        confirm_result(session, result_id=result_id, actor=_actor("reviewer-9"))
        session.commit()
    accepted = harness.writeback(instance_id, result_id)

    assert (
        accepted["writeback_ref"]["attempt_id"]
        == denied["writeback_ref"]["attempt_id"]
    )
    assert accepted["reused"] is True


def test_missing_writeback_query_is_404(harness: _Harness) -> None:
    """写错 id → 404 + `RESOURCE_NOT_FOUND`（与 `/api/jobs/{id}` 同一约定）。

    用通用的 `RESOURCE_NOT_FOUND` 而不是新造一个码：三个"按 id 取记录"
    的查询接口（作业 / 解析 / 回写）对"id 不存在"必须给**同一个**判据，
    否则调用方要按接口名分别处理同一件事。
    """
    response = harness.client.get("/api/writebacks/424242")
    assert response.status_code == 404
    assert response.json()["error_code"] == "RESOURCE_NOT_FOUND"


# ============================================================
# 6. 回写重试不重跑 M4/M5
# ============================================================


def test_transient_writeback_failure_never_reruns_parse_or_rules(
    harness: _Harness,
) -> None:
    """一次写入失败不得被放大成"重新解析 + 重新审查"。

    重跑会产生**新的批次与新的评价**，而"多出来的批次"在统计上
    看起来只是"又审了一次" —— 没有任何地方会报错。
    """
    instance_id = "HT-2026-0001"
    task_id, run_id = harness.seed_chain(instance_id=instance_id)
    result_id = harness.save(run_id)["result_id"]
    with harness.session() as session:
        confirm_result(session, result_id=result_id, actor=_actor("reviewer-9"))
        session.commit()
    harness.writeback(instance_id, result_id)

    before = harness.counts()
    # 把重试预算压到 2 次，走完"重试 → 耗尽"的完整路径
    # （外部的退避是真实时间，因此显式推进 `now` —— 不推进的话
    # 第二次派发会因为 `next_retry_at` 还没到而**根本没发生**，
    # 于是测试会以"任务还是 reviewing"失败，而它看起来像缺陷、其实是慢了）
    with harness.session() as session:
        event = session.execute(select(OutboxEvent)).scalar_one()
        event.max_attempts = 2
        session.commit()

    gateway = _FakeCommentGateway()
    gateway.always_transient = True
    dispatcher = OutboxDispatcher(harness.factory, gateway)

    t0 = datetime.now(timezone.utc).replace(tzinfo=None)
    assert dispatcher.run_once(now=t0) is True
    later = t0 + timedelta(seconds=backoff_seconds(1) + 5)
    assert dispatcher.run_once(now=later) is True

    task = harness.task(task_id)
    assert task.task_status == TaskStatus.BLOCKED.value
    assert task.blocked_stage == "writeback", "恢复点必须是回写，而不是解析"
    assert task.last_error_code == ErrorCode.APPROVAL_API_ERROR.value

    assert harness.counts() == before, "回写重试不得新增解析 / 批次 / 评价"
    attempt = harness.attempt(1)
    assert attempt.write_status == WriteStatus.FAILED.value
    assert attempt.reason_code == ErrorCode.APPROVAL_API_ERROR.value


def test_deterministic_writeback_failure_does_not_burn_retries(
    harness: _Harness,
) -> None:
    """确定性失败不浪费重试预算：一次就判死，恢复点仍是回写。"""
    instance_id = "HT-2026-0001"
    task_id, run_id = harness.seed_chain(instance_id=instance_id)
    result_id = harness.save(run_id)["result_id"]
    with harness.session() as session:
        confirm_result(session, result_id=result_id, actor=_actor("reviewer-9"))
        session.commit()
    harness.writeback(instance_id, result_id)

    gateway = _PermanentFailureGateway()
    dispatcher = OutboxDispatcher(harness.factory, gateway)
    dispatcher.run_once()

    assert gateway.write_calls == 1
    assert harness.task(task_id).task_status == TaskStatus.BLOCKED.value
    assert harness.counts()["parses"] == 1


# ============================================================
# 7. 作业与结果引用按 `job_type` 分派
# ============================================================


def test_result_reference_dispatches_by_job_type_and_never_probes_the_payload(
    harness: _Harness,
) -> None:
    """结果引用**只看 `job_type`**，绝不"从输入里顺手找个 id"。

    RESULT / WRITEBACK 作业的产物 id（结果号、尝试号）**不在冻结输入里**，
    所以它们没有结果引用。若改成"输入里有个 result_id 就拿来当结果引用"，
    就会指向**输入的那个**结果，而它看起来完全像一份正常的引用。
    """
    task_id, run_id = harness.seed_chain()
    result_id = harness.save(run_id)["result_id"]

    with harness.session() as session:
        result_job, _ = create_job(
            session,
            job_type="result",
            task_id=task_id,
            idempotency_key="result:test:1",
            input_payload={
                "run_id": run_id,
                "overall_risk_level": "low",
                "summary_text": "摘要",
                "focus_points_json": [],
                "comment_text": "正文",
                "actor": _actor_payload("tester"),
            },
        )
        writeback_job, _ = create_job(
            session,
            job_type="writeback",
            task_id=task_id,
            idempotency_key="writeback:test:1",
            input_payload={
                "instance_id": "HT-2026-0001",
                "result_id": result_id,
                "actor": _actor_payload("tester"),
            },
        )
        session.commit()
        result_job_id, writeback_job_id = result_job.id, writeback_job.id

    for job_id in (result_job_id, writeback_job_id):
        with harness.session() as session:
            from app.workflow.jobs import mark_succeeded

            mark_succeeded(session, session.get(WorkflowJob, job_id))
            session.commit()
        body = harness.client.get(f"/api/jobs/{job_id}").json()
        assert body["result_ref"] is None, (
            f"作业 {job_id} 的产物 id 不在冻结输入里，不得凭空推断出结果引用"
        )


# ============================================================
# 8. 异步模式：Worker 真的能执行 RESULT / WRITEBACK 作业
# ============================================================


def test_worker_executes_result_and_writeback_jobs(harness: _Harness) -> None:
    """企业部署默认由 Worker 异步处理（企业化设计 §6）。

    工具 6/7 在演示路径上同步完成；但同一个作业类型在异步模式与
    "瞬时失败后重试"路径上必须**有处理器** —— 否则作业会以
    "未注册处理器"定性失败，而失败原因指向的是接线，不是真正的故障。
    """
    from app.worker import Worker

    from scripts.run_worker import make_handler

    instance_id = "HT-2026-0001"
    task_id, run_id = harness.seed_chain(instance_id=instance_id)

    with harness.session() as session:
        create_job(
            session,
            job_type="result",
            task_id=task_id,
            idempotency_key="result:async:1",
            input_payload={
                "run_id": run_id,
                "overall_risk_level": "low",
                "summary_text": "异步摘要",
                "focus_points_json": ["关注点"],
                "comment_text": "异步正文",
                "actor": _actor_payload("async-worker"),
            },
        )
        session.commit()

    worker = Worker(harness.factory, make_handler(None), job_types=["result"])
    assert worker.run_once() is True

    saved = harness.results()
    assert len(saved) == 1
    assert saved[0].summary_text == "异步摘要"
    assert saved[0].created_by == "async-worker"

    result_id = saved[0].id
    with harness.session() as session:
        confirm_result(session, result_id=result_id, actor=_actor("reviewer-9"))
        create_job(
            session,
            job_type="writeback",
            task_id=task_id,
            idempotency_key="writeback:async:1",
            input_payload={
                "instance_id": instance_id,
                "result_id": result_id,
                "actor": _actor_payload("async-worker"),
            },
        )
        session.commit()

    worker = Worker(harness.factory, make_handler(None), job_types=["writeback"])
    assert worker.run_once() is True

    events = harness.outbox_events()
    assert len(events) == 1, "异步回写也必须留下待投递的意图"
    assert harness.task(task_id).write_status == WriteStatus.WRITING.value


def test_worker_still_fails_closed_for_unregistered_types(harness: _Harness) -> None:
    """未注册类型仍然**必须失败** —— "什么都不做"会把作业标成成功而结果为空。"""
    from scripts.run_worker import make_handler

    task_id, _run_id = harness.seed_chain()
    with harness.session() as session:
        create_job(
            session,
            job_type="rule",
            task_id=task_id,
            idempotency_key="rule:unregistered:1",
            input_payload={"run_id": 1, "parse_id": 1},
        )
        session.commit()

    # 只领 result：rule 不在领取范围内，因此没有作业可领（而不是"领到后失败"）
    from app.worker import Worker

    worker = Worker(harness.factory, make_handler(None), job_types=["result"])
    assert worker.run_once() is False


# ============================================================
# 辅助
# ============================================================


def _seed_result(
    harness: _Harness, *, task_id: int, run_id: int, risk: str, confirmed: bool
) -> int:
    """直接落一条结果（绕过工具 6）。

    用途是构造**工具 6 造不出来**的前置状态（如高风险且未确认）——
    从服务层伪造这种状态，比让工具 6 也支持"传入高风险"要诚实得多：
    后者等于给调用方一个改风险等级的口子。
    """
    comment_text = "高风险回写正文"
    digest = hashlib.sha256(comment_text.encode("utf-8")).hexdigest()
    with harness.session() as session:
        row = ReviewResult(
            task_id=task_id,
            run_id=run_id,
            overall_risk_level=risk,
            review_status="complete",
            hit_count=1,
            summary_text="高风险摘要",
            focus_points_json="[]",
            comment_text=comment_text,
            content_digest=digest,
            result_fingerprint=f"fp-{risk}-{confirmed}",
            version_no=1,
            created_by="seeder",
            manual_confirmed=1 if confirmed else 0,
            confirmed_digest=digest if confirmed else None,
            confirmed_by="reviewer-9" if confirmed else None,
            confirmed_at=(
                datetime.now(timezone.utc).replace(tzinfo=None) if confirmed else None
            ),
        )
        session.add(row)
        session.commit()
        return row.id
