"""M6 结构测试：Outbox 事件、不可变审计事件与结果版本化。

为什么这组测试存在：

1. **Outbox 是"先落库、后派发"的回写真相源**（规则 9：不得把 Redis 当业务真相）。
   少一条约束（状态取值域、尝试次数非负、幂等键非空唯一），
   "恰好一次回写"就退化成"大概率一次"——而重复回写的后果是审批人看到两份意见。

2. **`audit_events` 是只追加的审计账**（Task 1：无 update/delete 服务接口）。
   审计的价值在于**事后不可改**；结构上必须先证明它挂靠任务、
   动作取值受控，后续 Task 才能在其上叠加"不可变断言"。

3. **`review_results` 版本化**：同一任务的第 N 版结果、同一批次的同指纹结果
   都只能有一条；新版本必须通过 `supersedes_result_id` 显式接替旧版本，
   且**不得跨任务接替**（与 review_runs / rule_hits 的跨任务拼接防护同一理由）。

4. **RESULT / WRITEBACK 作业输入必须严格校验**（拒绝未知字段）：
   输入由外部调用方生成，键名拼错若被静默放行，
   作业会"照常成功"，只是用错的数据跑了一遍。
"""

from __future__ import annotations

import sqlite3

import pytest

from app.enums import AuditAction, JobType, OutboxEventType, OutboxStatus
from app.workflow.job_inputs import (
    INPUT_MODELS,
    PENDING_JOB_TYPES,
    validate_job_input,
)

SCHEMA_FILE_PATH = None  # 结构比对统一走 schema_conn，不需要额外文件句柄


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
# 辅助（与 test_schema_consistency.py 同一套种子手法）
# ============================================================


def _seed_task_with_parse(
    conn: sqlite3.Connection, approval_code: str
) -> tuple[int, int, int]:
    conn.execute(
        "INSERT INTO approval_tasks (approval_code, instance_id, context_status) "
        "VALUES (?, ?, 'complete')",
        (approval_code, approval_code),
    )
    task_id = conn.execute(
        "SELECT id FROM approval_tasks WHERE approval_code = ?", (approval_code,)
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO approval_attachments (task_id, attachment_id, file_name) "
        "VALUES (?, 'A-001', 'sample.pdf')",
        (task_id,),
    )
    attachment_id = conn.execute(
        "SELECT id FROM approval_attachments WHERE task_id = ?", (task_id,)
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO contract_parses "
        "(task_id, attachment_id, parse_status, parse_version, source_checksum) "
        "VALUES (?, ?, 'succeeded', 1, NULL)",
        (task_id, attachment_id),
    )
    parse_id = conn.execute(
        "SELECT id FROM contract_parses WHERE attachment_id = ?", (attachment_id,)
    ).fetchone()[0]
    return task_id, attachment_id, parse_id


def _seed_run(conn: sqlite3.Connection, task_id: int, parse_id: int) -> int:
    conn.execute(
        "INSERT INTO review_runs (task_id, parse_id, version_no) VALUES (?, ?, 1)",
        (task_id, parse_id),
    )
    return conn.execute(
        "SELECT id FROM review_runs WHERE task_id = ?", (task_id,)
    ).fetchone()[0]


def _seed_result(
    conn: sqlite3.Connection,
    task_id: int,
    run_id: int,
    *,
    version_no: int = 1,
    fingerprint: str = "fp-1",
    supersedes: int | None = None,
) -> int:
    conn.execute(
        "INSERT INTO review_results "
        "(task_id, run_id, overall_risk_level, version_no, result_fingerprint, "
        "supersedes_result_id) VALUES (?, ?, 'low', ?, ?, ?)",
        (task_id, run_id, version_no, fingerprint, supersedes),
    )
    return conn.execute(
        "SELECT id FROM review_results WHERE task_id = ? AND version_no = ?",
        (task_id, version_no),
    ).fetchone()[0]


def _seed_outbox(
    conn: sqlite3.Connection,
    *,
    key: str = "outbox-1",
    status: str = "pending",
    attempt: int = 0,
    max_attempts: int = 5,
    aggregate_type: str = "comment_log",
    event_type: str = "WRITE_APPROVAL_COMMENT",
) -> int:
    conn.execute(
        "INSERT INTO outbox_events "
        "(aggregate_type, aggregate_id, event_type, payload_json, idempotency_key, "
        "event_status, attempt_no, max_attempts) "
        "VALUES (?, 1, ?, '{}', ?, ?, ?, ?)",
        (aggregate_type, event_type, key, status, attempt, max_attempts),
    )
    return conn.execute(
        "SELECT id FROM outbox_events WHERE idempotency_key = ?", (key,)
    ).fetchone()[0]


def _seed_audit(
    conn: sqlite3.Connection,
    task_id: int,
    *,
    action: str = "RESULT_CONFIRMED",
    actor_name: str = "reviewer-1",
    target_type: str = "review_result",
) -> int:
    conn.execute(
        "INSERT INTO audit_events "
        "(task_id, actor_id, actor_name, action, target_type, target_id) "
        "VALUES (?, 'u-1', ?, ?, ?, 1)",
        (task_id, actor_name, action, target_type),
    )
    return conn.execute("SELECT id FROM audit_events").fetchone()[0]


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _index_columns(conn: sqlite3.Connection, table: str) -> set[tuple[str, ...]]:
    """返回该表所有索引的列元组集合（用于断言"领取/重试字段有索引"）。"""
    result: set[tuple[str, ...]] = set()
    for row in conn.execute(f"PRAGMA index_list({table})"):
        index_name = row[1]
        cols = tuple(
            r[2] for r in conn.execute(f"PRAGMA index_info({index_name})")
        )
        result.add(cols)
    return result


# ============================================================
# 1. outbox_events 结构与约束
# ============================================================


def test_outbox_events_table_exists_with_required_columns(
    schema_conn: sqlite3.Connection,
) -> None:
    """M6 计划 Task 1 规定的完整列集：缺任何一列，派发器就没有可用的状态载体。"""
    assert "outbox_events" in {
        row[0]
        for row in schema_conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert {
        "id",
        "aggregate_type",
        "aggregate_id",
        "event_type",
        "payload_json",
        "idempotency_key",
        "event_status",
        "attempt_no",
        "max_attempts",
        "next_retry_at",
        "lease_owner",
        "lease_expires_at",
        "last_error_code",
        "last_error_text",
        "correlation_id",
        "created_at",
        "delivered_at",
    } == _columns(schema_conn, "outbox_events")


def test_outbox_event_status_is_constrained(schema_conn: sqlite3.Connection) -> None:
    """状态只能取 pending / delivered / failed —— 第四个值会让对账口径分裂。"""
    _seed_outbox(schema_conn)  # pending 合法
    with pytest.raises(sqlite3.IntegrityError):
        _seed_outbox(schema_conn, key="outbox-2", status="maybe")
    schema_conn.rollback()


def test_outbox_attempts_are_bounded_and_non_negative(
    schema_conn: sqlite3.Connection,
) -> None:
    """attempt_no >= 0、max_attempts >= 1：负数 / 零次上限让退避重试失去意义。"""
    _seed_outbox(schema_conn, attempt=0, max_attempts=1)  # 边界合法

    with pytest.raises(sqlite3.IntegrityError):
        _seed_outbox(schema_conn, key="outbox-neg", attempt=-1)
    schema_conn.rollback()

    with pytest.raises(sqlite3.IntegrityError):
        _seed_outbox(schema_conn, key="outbox-zero-max", max_attempts=0)
    schema_conn.rollback()


def test_outbox_idempotency_key_non_empty_and_unique(
    schema_conn: sqlite3.Connection,
) -> None:
    """幂等键非空 + 唯一：这是"同一外部效果只发生一次"的数据库级防线。"""
    with pytest.raises(sqlite3.IntegrityError):
        _seed_outbox(schema_conn, key="   ")
    schema_conn.rollback()

    _seed_outbox(schema_conn, key="outbox-dup")
    with pytest.raises(sqlite3.IntegrityError):
        _seed_outbox(schema_conn, key="outbox-dup")
    schema_conn.rollback()


def test_outbox_event_type_and_aggregate_type_non_empty(
    schema_conn: sqlite3.Connection,
) -> None:
    """空事件类型 / 空聚合类型 = "不知道要派发什么"，必须在库层拒绝。"""
    with pytest.raises(sqlite3.IntegrityError):
        _seed_outbox(schema_conn, key="outbox-t1", event_type="  ")
    schema_conn.rollback()

    with pytest.raises(sqlite3.IntegrityError):
        _seed_outbox(schema_conn, key="outbox-t2", aggregate_type="")
    schema_conn.rollback()


def test_outbox_delivered_at_matches_status(schema_conn: sqlite3.Connection) -> None:
    """delivered_at 只属于 delivered 状态 —— 否则"送达时间"可被伪造。"""
    # 合法路径：同一次 UPDATE 同时改状态与时间（先验证，避免回滚把行撤掉）
    outbox_id = _seed_outbox(schema_conn, key="outbox-d1")
    schema_conn.execute(
        "UPDATE outbox_events SET event_status = 'delivered', "
        "delivered_at = CURRENT_TIMESTAMP WHERE id = ?",
        (outbox_id,),
    )
    schema_conn.rollback()

    # pending 行不得携带 delivered_at
    outbox_id = _seed_outbox(schema_conn, key="outbox-d2")
    with pytest.raises(sqlite3.IntegrityError):
        schema_conn.execute(
            "UPDATE outbox_events SET delivered_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (outbox_id,),
        )
    schema_conn.rollback()

    # delivered 行必须有 delivered_at（插入时就要带上）
    with pytest.raises(sqlite3.IntegrityError):
        _seed_outbox(schema_conn, key="outbox-d3", status="delivered")
    schema_conn.rollback()


def test_outbox_has_claim_and_retry_indexes(schema_conn: sqlite3.Connection) -> None:
    """领取扫描（event_status + next_retry_at）必须有索引。

    派发器是常驻轮询：没有索引时每次空扫描都退化为全表扫，
    Outbox 积压越多轮询越慢，最终表现成"回写延迟无故变大"。
    """
    indexes = _index_columns(schema_conn, "outbox_events")
    assert ("event_status", "next_retry_at") in indexes
    # 聚合定位（"这个业务对象有哪些待发事件"）同样需要索引
    assert ("aggregate_type", "aggregate_id") in indexes


def test_outbox_payload_is_required(schema_conn: sqlite3.Connection) -> None:
    """payload 非空：派发时要还原"当时要写什么"，留白就没有还原依据。"""
    with pytest.raises(sqlite3.IntegrityError):
        schema_conn.execute(
            "INSERT INTO outbox_events "
            "(aggregate_type, aggregate_id, event_type, payload_json, "
            "idempotency_key, event_status) "
            "VALUES ('comment_log', 1, 'WRITE_APPROVAL_COMMENT', '', 'outbox-p', 'pending')"
        )
    schema_conn.rollback()


# ============================================================
# 2. audit_events 结构与约束
# ============================================================


def test_audit_events_table_exists_with_required_columns(
    schema_conn: sqlite3.Connection,
) -> None:
    assert {
        "id",
        "task_id",
        "actor_id",
        "actor_name",
        "action",
        "target_type",
        "target_id",
        "correlation_id",
        "detail_json",
        "created_at",
    } == _columns(schema_conn, "audit_events")


def test_audit_events_belong_to_a_task_with_cascade(
    schema_conn: sqlite3.Connection,
) -> None:
    """审计事件必须挂靠真实任务，且随任务删除级联清理（与 task_logs 同语义）。"""
    task_id, _, _ = _seed_task_with_parse(schema_conn, "AUD-1")
    _seed_audit(schema_conn, task_id)

    # 级联清理必须在回滚撤销种子**之前**验证
    schema_conn.execute("DELETE FROM approval_tasks WHERE id = ?", (task_id,))
    assert (
        schema_conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 0
    )

    with pytest.raises(sqlite3.IntegrityError):
        _seed_audit(schema_conn, 999999)
    schema_conn.rollback()


def test_audit_action_is_constrained(schema_conn: sqlite3.Connection) -> None:
    """动作取值受控：自由文本的审计账没法按动作聚合统计。"""
    task_id, _, _ = _seed_task_with_parse(schema_conn, "AUD-2")
    _seed_audit(schema_conn, task_id, action="RESULT_CONFIRMED")

    with pytest.raises(sqlite3.IntegrityError):
        _seed_audit(schema_conn, task_id, action="DID_SOMETHING")
    schema_conn.rollback()


def test_audit_actor_and_target_must_be_present(
    schema_conn: sqlite3.Connection,
) -> None:
    """actor_name / target_type / target_id 非空缺一不可。

    没有操作者的审计不构成追责证据；没有目标则无法回答"确认的是哪一条"。
    ⚠️ 每段前都要重新种子：上一段的 rollback 会把种子一并撤销，
    否则下一段会因外键（而非目标 CHECK）通过，断言就名存实亡。
    """
    task_id, _, _ = _seed_task_with_parse(schema_conn, "AUD-3")
    with pytest.raises(sqlite3.IntegrityError):
        _seed_audit(schema_conn, task_id, actor_name="  ")
    schema_conn.rollback()

    task_id, _, _ = _seed_task_with_parse(schema_conn, "AUD-3b")
    with pytest.raises(sqlite3.IntegrityError):
        _seed_audit(schema_conn, task_id, target_type="")
    schema_conn.rollback()

    task_id, _, _ = _seed_task_with_parse(schema_conn, "AUD-3c")
    with pytest.raises(sqlite3.IntegrityError):
        schema_conn.execute(
            "INSERT INTO audit_events "
            "(task_id, actor_name, action, target_type) "
            "VALUES (?, 'u', 'RESULT_CONFIRMED', 'review_result')",
            (task_id,),
        )
    schema_conn.rollback()


# ============================================================
# 3. review_results 版本化
# ============================================================


def test_review_results_has_m6_version_columns(
    schema_conn: sqlite3.Connection,
) -> None:
    """版本化五件套：version_no / result_fingerprint / supersedes_result_id /
    created_by / updated_at。"""
    assert {
        "version_no",
        "result_fingerprint",
        "supersedes_result_id",
        "created_by",
        "updated_at",
    } <= _columns(schema_conn, "review_results")


def test_result_version_unique_per_task(schema_conn: sqlite3.Connection) -> None:
    """同一任务同一版本号只能有一条 —— 否则"当前版本"没有答案。"""
    task_id, _, parse_id = _seed_task_with_parse(schema_conn, "VER-1")
    run_id = _seed_run(schema_conn, task_id, parse_id)

    _seed_result(schema_conn, task_id, run_id, version_no=1, fingerprint="fp-a")
    with pytest.raises(sqlite3.IntegrityError):
        _seed_result(schema_conn, task_id, run_id, version_no=1, fingerprint="fp-b")
    schema_conn.rollback()


def test_result_fingerprint_unique_per_run(schema_conn: sqlite3.Connection) -> None:
    """同批次同指纹只能有一条：指纹相同即"内容与输入完全一致"，重放必须复用。

    跨任务允许同指纹（幂等判据按批次隔离，不跨批次约束）。
    """
    task_a, _, parse_a = _seed_task_with_parse(schema_conn, "VER-2")
    run_a = _seed_run(schema_conn, task_a, parse_a)
    task_b, _, parse_b = _seed_task_with_parse(schema_conn, "VER-3")
    run_b = _seed_run(schema_conn, task_b, parse_b)

    # 不同任务的同指纹各自成立（先验证，回滚会把种子一并撤销）
    _seed_result(schema_conn, task_a, run_a, fingerprint="fp-same")
    _seed_result(schema_conn, task_b, run_b, fingerprint="fp-same")

    with pytest.raises(sqlite3.IntegrityError):
        _seed_result(schema_conn, task_a, run_a, version_no=2, fingerprint="fp-same")
    schema_conn.rollback()


def test_result_version_and_fingerprint_are_valid(
    schema_conn: sqlite3.Connection,
) -> None:
    """version_no >= 1、指纹非空：版本 0 与空指纹都会让幂等判据失真。

    ⚠️ 两段各自重新种子：上段的 rollback 会撤销种子，
    否则下段会因外键而非目标 CHECK 抛错，断言名存实亡。
    """
    task_id, _, parse_id = _seed_task_with_parse(schema_conn, "VER-4")
    run_id = _seed_run(schema_conn, task_id, parse_id)
    with pytest.raises(sqlite3.IntegrityError):
        _seed_result(schema_conn, task_id, run_id, version_no=0)
    schema_conn.rollback()

    task_id, _, parse_id = _seed_task_with_parse(schema_conn, "VER-4b")
    run_id = _seed_run(schema_conn, task_id, parse_id)
    with pytest.raises(sqlite3.IntegrityError):
        _seed_result(schema_conn, task_id, run_id, fingerprint="  ")
    schema_conn.rollback()


def test_supersession_cannot_cross_tasks(schema_conn: sqlite3.Connection) -> None:
    """新版本只能接替**本任务**的旧结果 —— 与 review_runs 的跨任务拼接防护同理由。"""
    task_a, _, parse_a = _seed_task_with_parse(schema_conn, "SUP-1")
    run_a = _seed_run(schema_conn, task_a, parse_a)
    task_b, _, parse_b = _seed_task_with_parse(schema_conn, "SUP-2")
    run_b = _seed_run(schema_conn, task_b, parse_b)

    result_b = _seed_result(schema_conn, task_b, run_b)

    with pytest.raises(sqlite3.IntegrityError):
        _seed_result(
            schema_conn, task_a, run_a, version_no=2, supersedes=result_b
        )
    schema_conn.rollback()


# ============================================================
# 4. 枚举与作业输入
# ============================================================


def test_outbox_and_audit_enums() -> None:
    """OutboxStatus / OutboxEventType / AuditAction 的取值域。"""
    assert {status.value for status in OutboxStatus} == {
        "pending",
        "delivered",
        "failed",
    }
    assert {event.value for event in OutboxEventType} == {
        "WRITE_APPROVAL_COMMENT",
    }
    assert {action.value for action in AuditAction} == {
        "RESULT_CONFIRMED",
        # M7 增补：人工确认**权威审查上下文**（我方立场）。
        # 需求 §12 把"上下文确认"与"结果确认"并列为两类审计事件 ——
        # 它们是两个动作，不是一个动作的两个阶段。
        "CONTEXT_CONFIRMED",
        "WRITEBACK_REQUESTED",
        "WRITEBACK_DELIVERED",
        # M7 增补：人工重试（从失败检查点恢复）。不计它就无法回答
        # "这条任务为什么从 blocked 变回了 reviewing"。
        "TASK_RETRIED",
        # M7 增补：规则变更（需求 §12 明确要求，且**不属于任何任务** ——
        # 这正是 `audit_events.task_id` 改为可空的原因）。
        "RULE_CREATED",
        "RULE_UPDATED",
    }


def test_audit_event_may_belong_to_no_task(schema_conn: sqlite3.Connection) -> None:
    """系统级审计事件（`task_id` 为空）必须允许入库。

    ⚠️ 需求 §12 要求"规则修改"也进不可变审计账，而改一条规则影响的是
    **所有任务**。为它随便挑一个 `task_id`，审计里就会出现一条
    "看起来在说某条任务"的规则变更记录 —— 排障的人会去查那条任务，
    而真正变的是全局配置。
    """
    schema_conn.execute(
        "INSERT INTO audit_events (task_id, actor_id, actor_name, action, "
        "target_type, target_id) "
        "VALUES (NULL, 'u-1', 'admin', 'RULE_UPDATED', 'review_rule', 7)"
    )
    row = schema_conn.execute(
        "SELECT task_id, action FROM audit_events WHERE target_id = 7"
    ).fetchone()
    assert row is not None
    assert row[0] is None, "系统级事件不应被强行挂到某条任务上"
    assert row[1] == "RULE_UPDATED"
    schema_conn.rollback()


def test_result_and_writeback_job_inputs_are_strict() -> None:
    """RESULT / WRITEBACK 必须有严格输入模型，并从"待补"名单移除。"""
    assert JobType.RESULT in INPUT_MODELS
    assert JobType.WRITEBACK in INPUT_MODELS
    assert JobType.RESULT not in PENDING_JOB_TYPES
    assert JobType.WRITEBACK not in PENDING_JOB_TYPES


def test_result_job_input_rejects_unknown_fields() -> None:
    validated = validate_job_input(
        JobType.RESULT,
        {
            "run_id": 1,
            "overall_risk_level": "low",
            "summary_text": "摘要",
            "focus_points_json": ["关注点"],
            "comment_text": "正文",
            "actor": _actor_payload("reviewer-1"),
        },
    )
    assert validated["run_id"] == 1

    with pytest.raises(Exception, match="未知|unknown|extra"):
        validate_job_input(
            JobType.RESULT,
            {
                "run_id": 1,
                "overall_risk_level": "low",
                "summary_text": "摘要",
                "focus_points_json": ["关注点"],
                "comment_text": "正文",
                "actor": _actor_payload("reviewer-1"),
                "sourc_checksum": "拼错的键",
            },
        )


def test_writeback_job_input_rejects_unknown_fields() -> None:
    validated = validate_job_input(
        JobType.WRITEBACK,
        {"instance_id": "HT-1", "result_id": 1, "actor": _actor_payload("reviewer-1")},
    )
    assert validated["instance_id"] == "HT-1"

    with pytest.raises(Exception, match="未知|unknown|extra"):
        validate_job_input(
            JobType.WRITEBACK,
            {"instance_id": "HT-1", "result_id": 1, "actor": _actor_payload("u"), "extra": 1},
        )
