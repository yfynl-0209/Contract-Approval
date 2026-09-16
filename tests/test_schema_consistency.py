"""结构一致性、数据库约束与跨任务拼接防护测试。

为什么需要这组测试：

1. **防止 schema.sql 与 models.py 漂移**
   方案 1 的代价是"表结构定义在两个地方"（手写 SQL + ORM 映射）。
   改一边忘一边就会出现"ORM 能写、SQL 里没这列"的隐蔽错误。
   本测试用**真实的 schema.sql** 建库，再与 ORM 元数据逐表逐列比对。

2. **验证外键真的生效**
   `PRAGMA foreign_keys` 是连接级设置。若只写在 schema.sql 里，
   应用运行时的新连接默认是 OFF，`ON DELETE CASCADE` 形同虚设。

3. **验证"跨任务拼接"在结构上不可能**
   `review_runs` / `rule_hits` / `review_results` / `comment_logs` 都同时保存
   `task_id` 与父级 id。只靠单列外键，数据库允许把任务 A 的记录挂到任务 B 的解析结果上。
   这些测试逐条证明复合外键拦住了它。

4. **锁死需求文档规定的字段名**——回归时不能被无意改动。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import inspect, text

import app.models  # noqa: F401  导入以触发全部 ORM 模型注册
from app.config import PROJECT_ROOT
from app.db import Base, engine
from app.enums import ErrorCode, WritebackReasonCode, WriteStatus, is_retryable
from app.models import RuleEvaluation

SCHEMA_FILE = PROJECT_ROOT / "db" / "schema.sql"

# 需求文档 2.4.9 漏列、但文档别处明确要求的字段（属"补全"而非"扩展"）
DOC_REQUIRED_COLUMNS: dict[str, set[str]] = {
    "approval_tasks": {"apply_time"},  # 2.4.3 待办列表要求"申请时间"
    "approval_attachments": {"attachment_id"},  # 2.4.10 下载工具入参
    "rule_hits": {"risk_level", "task_id"},  # 2.4.6 命中结果至少包含风险等级
    "comment_logs": {"review_id"},  # 2.4.10 write_approval_comment(review_id)
    "review_results": {"task_id"},
}

# 需求文档 2.4.9 规定的表名（本次新增 review_runs 为第 9 张表，见扩展清单）
DOC_TABLES = {
    "approval_tasks",
    "approval_attachments",
    "contract_parses",
    "review_rules",
    "rule_hits",
    "review_results",
    "comment_logs",
    "task_logs",
}


# ============================================================
# 辅助
# ============================================================


def _schema_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%'"
    )
    return {row[0] for row in rows}


def _schema_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    # PRAGMA table_info 返回 (cid, name, type, notnull, dflt_value, pk)
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _seed_task_with_parse(
    conn: sqlite3.Connection, approval_code: str
) -> tuple[int, int, int]:
    """建一条任务 + 附件 + 解析记录，返回 (task_id, attachment_id, parse_id)。

    注意解析记录必须带 `attachment_id`（NOT NULL）——
    否则 `UNIQUE(attachment_id, parse_version)` 会因 SQLite 视 NULL 互不相同而失效。
    """
    # instance_id 是 NOT NULL（去重键组成），必须显式给。
    # 演示数据里它与 approval_code 同值——符合"审批单编号即外部实例号"的实际情形。
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
        "VALUES (?, ?, ?)",
        (task_id, "A-001", "sample.pdf"),
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


def _seed_result(conn: sqlite3.Connection, task_id: int, run_id: int) -> int:
    conn.execute(
        "INSERT INTO review_results "
        "(task_id, run_id, overall_risk_level, version_no, result_fingerprint) "
        "VALUES (?, ?, 'low', 1, 'fp-seed')",
        (task_id, run_id),
    )
    return conn.execute(
        "SELECT id FROM review_results WHERE run_id = ?", (run_id,)
    ).fetchone()[0]


# ============================================================
# 1. 结构一致性
# ============================================================


def test_table_count_is_thirteen(schema_conn: sqlite3.Connection) -> None:
    """8 张需求表 + review_runs（M1.5）+ workflow_jobs（M3）+ parse_artifacts（M4）
    + outbox_events / audit_events（M6）。

    `workflow_jobs` 在 M3 就建立而非"提前建空表"：
    它的 `idempotency_key` 在 M3 就是工具 1~3 重放请求的幂等台账，
    M4 引入 Worker 时表结构与写入路径都不需要改。

    `parse_artifacts` 是 M4 新增的第 11 张：标准文档与 OCR 原始工件体积大，
    只把对象键与摘要入库。表数写死在这里是**有意**的 ——
    它让"新增一张表"必须显式改这条断言，而不是悄悄多出来一张没人记得的表。

    M6 的第 12、13 张：`outbox_events`（回写事务性意图）与 `audit_events`
    （只追加审计账）。
    """
    assert len(_schema_tables(schema_conn)) == 13


def test_doc_required_tables_all_exist(schema_conn: sqlite3.Connection) -> None:
    """需求文档 2.4.9 规定的 8 张表一张都不能少。"""
    assert DOC_TABLES <= _schema_tables(schema_conn)


def test_schema_and_orm_have_same_tables(schema_conn: sqlite3.Connection) -> None:
    """schema.sql 与 models.py 的表集合必须完全一致（双向包含）。"""
    assert _schema_tables(schema_conn) == set(Base.metadata.tables)


@pytest.mark.parametrize("table", sorted(Base.metadata.tables))
def test_schema_and_orm_have_same_columns(
    schema_conn: sqlite3.Connection, table: str
) -> None:
    """逐表比对列名集合，防止两边漂移。"""
    orm_columns = set(Base.metadata.tables[table].columns.keys())
    assert _schema_columns(schema_conn, table) == orm_columns, (
        f"表 {table} 的 schema.sql 与 models.py 列不一致"
    )


def test_doc_required_columns_present(schema_conn: sqlite3.Connection) -> None:
    """需求文档别处要求、但 2.4.9 漏列的字段必须存在。"""
    for table, required in DOC_REQUIRED_COLUMNS.items():
        assert required <= _schema_columns(schema_conn, table), (
            f"表 {table} 缺少文档要求的字段：{required}"
        )


def test_evaluation_status_maps_to_physical_hit_status() -> None:
    """设计决议 D2：物理列名沿用 hit_status，Python 属性名用 evaluation_status，不产生重复列。"""
    columns = set(RuleEvaluation.__table__.columns.keys())
    assert "hit_status" in columns, "物理列名必须符合需求文档 2.4.9"
    assert "evaluation_status" not in columns, "不得新增重复列"

    prop = inspect(RuleEvaluation).get_property("evaluation_status")
    assert prop.columns[0].name == "hit_status"


def test_rule_hits_table_name_unchanged() -> None:
    """物理表名必须仍为 rule_hits（Python 类名可不同）。"""
    assert RuleEvaluation.__tablename__ == "rule_hits"


# ============================================================
# 2. 外键约束
# ============================================================


def test_sqlite_foreign_keys_enabled_on_new_connection() -> None:
    """引擎新建的连接上，foreign_keys 必须是开启状态。

    这是对 app/db.py 中 connect 事件监听器的回归保护：
    一旦监听器被误删，级联删除就会静默失效。
    """
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1


def test_cascade_delete_actually_works(work_dir: Path) -> None:
    """实测级联删除：删除任务后，其下**整条链**的记录都必须消失。"""
    conn = sqlite3.connect(work_dir / "cascade.db")
    try:
        conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
        conn.execute("PRAGMA foreign_keys=ON")

        conn.execute(
            "INSERT INTO approval_tasks (approval_code, instance_id, context_status) "
            "VALUES ('T-001', 'T-001', 'complete')"
        )
        task_id = conn.execute("SELECT id FROM approval_tasks").fetchone()[0]
        conn.execute(
            "INSERT INTO approval_attachments (task_id, attachment_id, file_name) "
            "VALUES (?, 'A-001', 'sample.pdf')",
            (task_id,),
        )
        attachment_id = conn.execute(
            "SELECT id FROM approval_attachments"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO contract_parses (task_id, attachment_id) VALUES (?, ?)",
            (task_id, attachment_id),
        )
        parse_id = conn.execute("SELECT id FROM contract_parses").fetchone()[0]
        conn.execute(
            "INSERT INTO review_runs (task_id, parse_id, version_no) VALUES (?, ?, 1)",
            (task_id, parse_id),
        )
        run_id = conn.execute("SELECT id FROM review_runs").fetchone()[0]
        rule_id = conn.execute(
            "INSERT INTO review_rules (rule_code, rule_name, match_mode, match_text) "
            "VALUES ('R1', '测试规则', 'keyword', '{\"keywords\": [\"x\"]}') RETURNING id"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO rule_hits (run_id, task_id, rule_id, risk_level) "
            "VALUES (?, ?, ?, 'high')",
            (run_id, task_id, rule_id),
        )
        conn.execute(
            "INSERT INTO review_results "
            "(task_id, run_id, overall_risk_level, version_no, result_fingerprint) "
            "VALUES (?, ?, 'high', 1, 'fp-cascade')",
            (task_id, run_id),
        )
        result_id = conn.execute("SELECT id FROM review_results").fetchone()[0]
        conn.execute(
            "INSERT INTO comment_logs (task_id, review_id, idempotency_key) "
            "VALUES (?, ?, 'K1')",
            (task_id, result_id),
        )

        conn.execute("DELETE FROM approval_tasks WHERE id = ?", (task_id,))

        for table in (
            "approval_attachments",
            "contract_parses",
            "review_runs",
            "rule_hits",
            "review_results",
            "comment_logs",
        ):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, (
                f"级联删除未清理 {table}"
            )
    finally:
        conn.close()


def test_foreign_key_definitions_are_valid(schema_conn: sqlite3.Connection) -> None:
    """自检：所有外键引用的表与列都必须真实存在（防手写 SQL 拼错表名）。"""
    tables = _schema_tables(schema_conn)
    problems: list[str] = []

    for table in sorted(tables):
        # PRAGMA foreign_key_list 返回
        # (id, seq, ref_table, from_col, to_col, on_update, on_delete, match)
        for fk in schema_conn.execute(f"PRAGMA foreign_key_list({table})"):
            ref_table, from_col, to_col = fk[2], fk[3], fk[4]
            if ref_table not in tables:
                problems.append(f"{table}.{from_col} → 不存在的表 {ref_table}")
                continue
            if to_col is None:
                # SQLite 在引用父表主键时 `to` 可能为 NULL
                continue
            if to_col not in _schema_columns(schema_conn, ref_table):
                problems.append(f"{table}.{from_col} → 不存在的列 {ref_table}.{to_col}")

    assert not problems, "外键定义有问题：" + "；".join(problems)


# ============================================================
# 3. 唯一约束
# ============================================================


def test_duplicate_instance_in_same_tenant_rejected(
    schema_conn: sqlite3.Connection,
) -> None:
    """同 provider + 同租户 + 同 instance_id → 拒绝。

    这是**对象级闸门**：同一审批单只能有一条任务记录（需求 2.4.4 去重的实现基础）。
    """
    schema_conn.execute(
        "INSERT INTO approval_tasks (approval_code, instance_id, context_status) "
        "VALUES ('DUP-1', 'DUP-1', 'complete')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        schema_conn.execute(
            "INSERT INTO approval_tasks (approval_code, instance_id, context_status) "
            "VALUES ('DUP-1', 'DUP-1', 'complete')"
        )
    schema_conn.rollback()


def test_same_approval_code_in_another_tenant_is_allowed(
    schema_conn: sqlite3.Connection,
) -> None:
    """同 `approval_code`、不同租户 → **必须允许**。

    这是"取消 approval_code 全局唯一"的核心反例：
    需求 2.4.4 只要求按唯一业务标识去重，**并未要求审批单编号跨企业、跨审批平台全局唯一**。
    若这里抛 IntegrityError，就意味着接入第二个企业时必然要迁移——
    这正是本次约束调整要消除的问题。
    """
    schema_conn.execute(
        "INSERT INTO approval_tasks (approval_code, instance_id, context_status) "
        "VALUES ('SHARED-CODE', 'SHARED-CODE', 'complete')"
    )
    schema_conn.execute(
        "INSERT INTO approval_tasks "
        "(provider, tenant_id, approval_code, instance_id, context_status) "
        "VALUES ('mock', 'tenant-b', 'SHARED-CODE', 'SHARED-CODE', 'complete')"
    )
    count = schema_conn.execute(
        "SELECT COUNT(*) FROM approval_tasks WHERE approval_code = 'SHARED-CODE'"
    ).fetchone()[0]
    assert count == 2, "不同租户应能各自持有相同编号的审批单"
    schema_conn.rollback()


def test_duplicate_workflow_job_idempotency_key_rejected(
    schema_conn: sqlite3.Connection,
) -> None:
    """作业幂等键唯一 —— **操作级闸门**：同一输入版本的操作不得重复入队。"""
    insert = (
        "INSERT INTO workflow_jobs (job_type, idempotency_key, input_json, input_digest) "
        "VALUES ('pull', 'pull:mock:default:202609131200', ?, ?)"
    )
    payload = ('{"provider":"mock","tenant_id":"default"}', "d" * 64)

    schema_conn.execute(insert, payload)
    with pytest.raises(sqlite3.IntegrityError):
        schema_conn.execute(insert, payload)
    schema_conn.rollback()


def test_workflow_job_requires_input(schema_conn: sqlite3.Connection) -> None:
    """`input_json` / `input_digest` 是 NOT NULL，且不得为空串。

    空输入等同于"没有输入"，会让这次作业永远无法回答"基于什么输入" ——
    而它恰恰是这两列存在的唯一用途。
    """
    with pytest.raises(sqlite3.IntegrityError):
        schema_conn.execute(
            "INSERT INTO workflow_jobs (job_type, idempotency_key, input_digest) "
            "VALUES ('pull', 'k-1', ?)",
            ("d" * 64,),
        )
    schema_conn.rollback()

    with pytest.raises(sqlite3.IntegrityError):
        schema_conn.execute(
            "INSERT INTO workflow_jobs (job_type, idempotency_key, input_json, input_digest) "
            "VALUES ('pull', 'k-2', '   ', ?)",
            ("d" * 64,),
        )
    schema_conn.rollback()


def test_same_job_type_with_new_version_is_allowed(
    schema_conn: sqlite3.Connection,
) -> None:
    """**同一对象、新输入版本 → 必须允许新作业**。

    这是"作业幂等键不得只含 instance_id"的核心反例：
    审批表单会变化，若幂等键只认审批单号，同一审批单的第二次同步会被**永久拒绝**。
    这就是把操作级闸门误当对象级闸门所导致的死锁。
    """
    insert = (
        "INSERT INTO workflow_jobs (job_type, idempotency_key, input_json, input_digest) "
        "VALUES ('detail', ?, ?, ?)"
    )
    payload = ('{"instance_id":"HT-2026-0001"}', "d" * 64)

    schema_conn.execute(insert, ("detail:HT-2026-0001:v1", *payload))
    schema_conn.execute(insert, ("detail:HT-2026-0001:v2", *payload))
    count = schema_conn.execute(
        "SELECT COUNT(*) FROM workflow_jobs WHERE job_type = 'detail'"
    ).fetchone()[0]
    assert count == 2, "详情变化后必须能产生新作业"
    schema_conn.rollback()


def test_duplicate_idempotency_key_rejected(schema_conn: sqlite3.Connection) -> None:
    """idempotency_key 唯一 —— 并发回写的最后一道防线。"""
    task_id, _, parse_id = _seed_task_with_parse(schema_conn, "IDEM-1")
    run_id = _seed_run(schema_conn, task_id, parse_id)
    result_id = _seed_result(schema_conn, task_id, run_id)

    schema_conn.execute(
        "INSERT INTO comment_logs (task_id, review_id, idempotency_key) "
        "VALUES (?, ?, 'KEY-1')",
        (task_id, result_id),
    )
    with pytest.raises(sqlite3.IntegrityError):
        schema_conn.execute(
            "INSERT INTO comment_logs (task_id, review_id, idempotency_key) "
            "VALUES (?, ?, 'KEY-1')",
            (task_id, result_id),
        )
    schema_conn.rollback()


def test_rule_evaluation_unique_per_run_and_rule(
    schema_conn: sqlite3.Connection,
) -> None:
    """同一批次内每条规则只能有一条评价 —— 四态记录不重复的前提。"""
    task_id, _, parse_id = _seed_task_with_parse(schema_conn, "EVAL-1")
    run_id = _seed_run(schema_conn, task_id, parse_id)

    # 该 fixture 只执行 schema.sql（不含种子数据），因此这里自建一条规则
    schema_conn.execute(
        "INSERT INTO review_rules (rule_code, rule_name, match_mode, match_text) "
        "VALUES (?, ?, ?, ?)",
        ("TEST_RULE", "测试规则", "keyword", '{"keywords": ["占位"]}'),
    )
    rule_id = schema_conn.execute(
        "SELECT id FROM review_rules WHERE rule_code = 'TEST_RULE'"
    ).fetchone()[0]

    schema_conn.execute(
        "INSERT INTO rule_hits (run_id, task_id, rule_id, risk_level, hit_status) "
        "VALUES (?, ?, ?, 'high', 'hit')",
        (run_id, task_id, rule_id),
    )
    with pytest.raises(sqlite3.IntegrityError):
        schema_conn.execute(
            "INSERT INTO rule_hits (run_id, task_id, rule_id, risk_level, hit_status) "
            "VALUES (?, ?, ?, 'high', 'not_hit')",
            (run_id, task_id, rule_id),
        )
    schema_conn.rollback()


def test_parse_version_unique_per_attachment(
    schema_conn: sqlite3.Connection,
) -> None:
    """同一附件的同一解析版本只能有一条记录。"""
    task_id, attachment_id, _ = _seed_task_with_parse(schema_conn, "PARSE-1")

    with pytest.raises(sqlite3.IntegrityError):
        schema_conn.execute(
            "INSERT INTO contract_parses (task_id, attachment_id, parse_version) "
            "VALUES (?, ?, 1)",
            (task_id, attachment_id),
        )
    schema_conn.rollback()


def test_same_checksum_from_different_attachments_is_allowed(
    schema_conn: sqlite3.Connection,
) -> None:
    """两个不同审批单上传**同一份模板合同**时必须都能记录解析结果。

    这是最初把唯一键写成 `UNIQUE(source_checksum, parse_version)` 的后果：
    内容相同 → 校验和相同 → 第二个任务插入解析记录时会被错误地拒绝。
    唯一键必须按"附件"区分，而不是按文件内容。
    """
    _, first_attachment, _ = _seed_task_with_parse(schema_conn, "CK-1")
    _, second_attachment, _ = _seed_task_with_parse(schema_conn, "CK-2")
    assert first_attachment != second_attachment

    same_checksum = "a" * 64
    # 两条解析记录分属不同附件、版本号都是 1、校验和相同（同一份模板合同）。
    # 唯一键若写成 UNIQUE(source_checksum, parse_version)，第二条会被错误拒绝。
    schema_conn.execute(
        "UPDATE contract_parses SET source_checksum = ? WHERE attachment_id IN (?, ?)",
        (same_checksum, first_attachment, second_attachment),
    )

    count = schema_conn.execute(
        "SELECT COUNT(*) FROM contract_parses WHERE source_checksum = ?",
        (same_checksum,),
    ).fetchone()[0]
    assert count == 2, "同一份模板合同在两个任务下必须都能记录解析结果"
    schema_conn.rollback()


def test_source_checksum_is_indexed(schema_conn: sqlite3.Connection) -> None:
    """source_checksum 仍需索引：它用于"内容是否变化"的快速判断。"""
    indexes = {
        row[1] for row in schema_conn.execute("PRAGMA index_list(contract_parses)")
    }
    assert "idx_parse_checksum" in indexes


# ============================================================
# 4. 跨任务拼接防护（复合外键）
# ============================================================


def test_run_rejects_parse_from_another_task(
    schema_conn: sqlite3.Connection,
) -> None:
    """批次不能使用**别的任务**的解析记录。"""
    task_a, _, _ = _seed_task_with_parse(schema_conn, "X-1")
    _, _, parse_b = _seed_task_with_parse(schema_conn, "X-2")

    with pytest.raises(sqlite3.IntegrityError):
        schema_conn.execute(
            "INSERT INTO review_runs (task_id, parse_id, version_no) "
            "VALUES (?, ?, 1)",
            (task_a, parse_b),
        )
    schema_conn.rollback()


def test_rule_evaluation_rejects_run_from_another_task(
    schema_conn: sqlite3.Connection,
) -> None:
    """规则评价不能挂到**别的任务**的批次上。"""
    task_a, _, parse_a = _seed_task_with_parse(schema_conn, "X-3")
    task_b, _, parse_b = _seed_task_with_parse(schema_conn, "X-4")
    run_b = _seed_run(schema_conn, task_b, parse_b)

    schema_conn.execute(
        "INSERT INTO review_rules (rule_code, rule_name, match_mode, match_text) "
        "VALUES ('R-X', '规则', 'keyword', '{\"keywords\": [\"x\"]}')"
    )
    rule_id = schema_conn.execute(
        "SELECT id FROM review_rules WHERE rule_code = 'R-X'"
    ).fetchone()[0]

    with pytest.raises(sqlite3.IntegrityError):
        schema_conn.execute(
            "INSERT INTO rule_hits (run_id, task_id, rule_id, risk_level) "
            "VALUES (?, ?, ?, 'high')",
            (run_b, task_a, rule_id),
        )
    schema_conn.rollback()


def test_review_result_rejects_run_from_another_task(
    schema_conn: sqlite3.Connection,
) -> None:
    """审查结果不能挂到**别的任务**的批次上。"""
    task_a, _, _ = _seed_task_with_parse(schema_conn, "X-5")
    task_b, _, parse_b = _seed_task_with_parse(schema_conn, "X-6")
    run_b = _seed_run(schema_conn, task_b, parse_b)

    with pytest.raises(sqlite3.IntegrityError):
        schema_conn.execute(
            "INSERT INTO review_results "
            "(task_id, run_id, overall_risk_level, version_no, result_fingerprint) "
            "VALUES (?, ?, 'high', 1, 'fp-x5')",
            (task_a, run_b),
        )
    schema_conn.rollback()


def test_comment_log_rejects_result_from_another_task(
    schema_conn: sqlite3.Connection,
) -> None:
    """回写日志不能指向**别的任务**的审查结果。"""
    task_a, _, parse_a = _seed_task_with_parse(schema_conn, "X-7")
    task_b, _, parse_b = _seed_task_with_parse(schema_conn, "X-8")
    run_b = _seed_run(schema_conn, task_b, parse_b)
    result_b = _seed_result(schema_conn, task_b, run_b)

    with pytest.raises(sqlite3.IntegrityError):
        schema_conn.execute(
            "INSERT INTO comment_logs (task_id, review_id, idempotency_key) "
            "VALUES (?, ?, 'K-X')",
            (task_a, result_b),
        )
    schema_conn.rollback()


# ============================================================
# 5. 回写状态与原因码的职责边界
# ============================================================


def test_write_status_has_exactly_four_values() -> None:
    """`write_status` 严格保持需求规定的四值，**不得**把门禁原因混进来。

    背景：曾短暂加过第五个值 `rejected` 表示"门禁不让回写"。
    但那会让同一个字段同时表达两个正交问题：

      - 「是否允许发起回写」——门禁的**前置判断**；
      - 「回写是否成功」——外部调用的**执行结果**。

    而门禁拒绝时**根本没有发起回写**，正确状态就是 `not_written`。
    门禁原因改由 `comment_logs.reason_code` 表达。

    这条测试是防止"为了让状态更好懂"而再次扩展该枚举。
    """
    assert {status.value for status in WriteStatus} == {
        "not_written",
        "writing",
        "success",
        "failed",
    }
    assert not hasattr(WriteStatus, "REJECTED"), (
        "不得重新引入 rejected：门禁拒绝不是一种回写状态"
    )


def test_writeback_reason_code_covers_both_dimensions() -> None:
    """原因码必须能区分「门禁拒绝」与「外部失败」，排障时才分得清责任方。

    这两类问题查的地方完全不同：
      门禁拒绝 → 查门禁配置与任务状态；
      外部失败 → 查审批系统与网络。
    """
    values = {code.value for code in WritebackReasonCode}

    # 门禁拒绝类（配合 write_status = not_written）
    assert "WRITEBACK_POLICY_DENIED" in values
    assert "CONTEXT_NOT_VALID" in values  # 立场上下文不可信
    assert "MANUAL_CONFIRM_REQUIRED" in values  # 高风险未人工确认

    # 外部失败类（配合 write_status = failed）
    assert "APPROVAL_API_ERROR" in values
    assert "APPROVAL_API_TIMEOUT" in values


def test_comment_logs_has_reason_code_columns(
    schema_conn: sqlite3.Connection,
) -> None:
    """门禁原因必须有独立字段落库。

    否则只能塞进 `write_response_text` —— 那是"外部系统返回的原始文本"，
    门禁拒绝时根本没有外部调用，放进去语义就错了。
    """
    assert {"reason_code", "reason_text"} <= _schema_columns(
        schema_conn, "comment_logs"
    )


# ============================================================
# 6. M3 数据地基：新增列、新表与错误码分类
# ============================================================


def test_approval_tasks_has_m3_columns(schema_conn: sqlite3.Connection) -> None:
    """M3 新增列必须存在。

    `instance_id` 刻意只加 NOT NULL 而**不给 DEFAULT**：
    默认空串会让 UNIQUE(provider, tenant_id, instance_id) 形同虚设——
    所有旧记录的空串都算"同一个"，去重约束反而失效。
    """
    assert {
        "provider",
        "tenant_id",
        "instance_id",
        "form_data_json",
        "blocked_stage",
        "last_error_code",
    } <= _schema_columns(schema_conn, "approval_tasks")


def test_attachments_has_object_storage_columns(
    schema_conn: sqlite3.Connection,
) -> None:
    """附件表必须能同时表达两种位置（§5.2）。

    `file_path`（受控临时物化路径）与 `object_key`（长期对象键）性质不同，
    合并成一个字段会让调用端有机会拿到永久地址。
    """
    assert {
        "object_key",
        "content_type",
        "file_path",
        "file_checksum",
    } <= _schema_columns(schema_conn, "approval_attachments")


def test_workflow_jobs_table_exists_with_checkpoint_columns(
    schema_conn: sqlite3.Connection,
) -> None:
    """作业表必须能表达内部状态、重试与检查点（§7.2 / §7.4）。"""
    assert {
        "job_type",
        "idempotency_key",
        "job_status",
        "attempt_no",
        "max_attempts",
        "next_retry_at",
        "checkpoint_json",
        "last_error_code",
    } <= _schema_columns(schema_conn, "workflow_jobs")


def test_contract_parses_has_m4_trace_columns(
    schema_conn: sqlite3.Connection,
) -> None:
    """解析版本追溯与缓存列必须存在（M4 设计文档 §3.2）。

    `parser_version`（由哪个解析器产生）与 `parse_version`（同一附件的第几次解析）
    是**两件事**。少了前者，解析器升级后缓存判定会命中旧记录，
    **升级后的解析器永远不会被真正执行**，而且没有任何报错。
    """
    assert {
        "parser_name",
        "parser_version",
        "config_digest",
        "cache_key",
        "parse_error_code",
    } <= _schema_columns(schema_conn, "contract_parses")


def test_workflow_jobs_has_m4_input_and_lease_columns(
    schema_conn: sqlite3.Connection,
) -> None:
    """不可变输入、关联 ID 与租约列必须存在（M4 设计文档 §3.3 / §4.4）。"""
    assert {
        "input_json",
        "input_digest",
        "correlation_id",
        "lease_owner",
        "lease_token",
        "lease_expires_at",
    } <= _schema_columns(schema_conn, "workflow_jobs")


def test_parse_artifacts_table_columns(schema_conn: sqlite3.Connection) -> None:
    """工件表只留对象键与摘要 —— 内容入对象存储（体积原因）。"""
    assert {
        "parse_id",
        "kind",
        "object_key",
        "sha256",
        "size_bytes",
        "content_type",
        "artifact_version",
    } <= _schema_columns(schema_conn, "parse_artifacts")


def test_parse_artifact_kind_is_constrained(schema_conn: sqlite3.Connection) -> None:
    """`kind` 只能取两类：标准文档与 OCR 原始结果。"""
    task_id, _, parse_id = _seed_task_with_parse(schema_conn, "ART-1")
    with pytest.raises(sqlite3.IntegrityError):
        schema_conn.execute(
            "INSERT INTO parse_artifacts "
            "(parse_id, kind, object_key, sha256, size_bytes) "
            "VALUES (?, 'unknown_kind', 'k', 's', 1)",
            (parse_id,),
        )
    schema_conn.rollback()


def test_artifact_cascades_with_parse(schema_conn: sqlite3.Connection) -> None:
    """删除任务必须连带清掉工件 —— 否则对象引用残留，指向已不存在的解析。"""
    task_id, _, parse_id = _seed_task_with_parse(schema_conn, "ART-2")
    schema_conn.execute(
        "INSERT INTO parse_artifacts (parse_id, kind, object_key, sha256, size_bytes) "
        "VALUES (?, 'standard_document', 'k1', 's1', 10)",
        (parse_id,),
    )

    schema_conn.execute("DELETE FROM approval_tasks WHERE id = ?", (task_id,))

    assert (
        schema_conn.execute("SELECT COUNT(*) FROM parse_artifacts").fetchone()[0] == 0
    )


# ============================================================
# 7. 缓存占位闸门（部分唯一索引，M4 §3.2）
# ============================================================


def _seed_parse_row(
    conn: sqlite3.Connection, task_id: int, attachment_id: int, *, status: str,
    version: int, cache_key: str = "K1",
) -> None:
    conn.execute(
        "INSERT INTO contract_parses "
        "(task_id, attachment_id, parse_status, parse_version, cache_key) "
        "VALUES (?, ?, ?, ?, ?)",
        (task_id, attachment_id, status, version, cache_key),
    )


def test_cache_gate_blocks_duplicate_in_flight_parse(
    schema_conn: sqlite3.Connection,
) -> None:
    """进行中的同键记录**必须**被挡住 —— 这是"并发只跑一次 OCR"的闸门。

    占位行是"这次解析已被认领"的凭证。少了它，两个并发请求会各跑一遍 OCR，
    而 OCR 恰恰是整条链路里最贵的一步。
    """
    task_id, attachment_id, _ = _seed_task_with_parse(schema_conn, "GATE-1")
    _seed_parse_row(schema_conn, task_id, attachment_id, status="pending", version=2)

    with pytest.raises(sqlite3.IntegrityError):
        _seed_parse_row(
            schema_conn, task_id, attachment_id, status="pending", version=3
        )
    schema_conn.rollback()


def test_cache_gate_allows_reparse_after_failure(
    schema_conn: sqlite3.Connection,
) -> None:
    """**失败记录不占位**：`failed` 之后必须能重新解析。

    这条是"部分唯一索引"存在的**全部理由**。写成全局唯一时，
    下面这次 INSERT 会抛 `IntegrityError` —— 也就是说
    「并发只跑一次 OCR」与「失败后可以重新解析」**互斥**，
    而两者都是明确要求。加上 `WHERE parse_status IN (...)` 之后才自洽。
    """
    task_id, attachment_id, _ = _seed_task_with_parse(schema_conn, "GATE-2")
    schema_conn.execute(
        "UPDATE contract_parses SET cache_key = 'K1', parse_status = 'failed' "
        "WHERE attachment_id = ?",
        (attachment_id,),
    )

    # 失败之后重来：同附件、同 cache_key、状态 pending → 必须允许
    _seed_parse_row(schema_conn, task_id, attachment_id, status="pending", version=2)

    count = schema_conn.execute(
        "SELECT COUNT(*) FROM contract_parses WHERE attachment_id = ?",
        (attachment_id,),
    ).fetchone()[0]
    assert count == 2, "失败记录必须保留（历史留痕），且不阻止重新解析"


def test_cache_gate_blocks_duplicate_succeeded_parse(
    schema_conn: sqlite3.Connection,
) -> None:
    """已成功的同键记录同样占位 —— 那正是"缓存命中"要复用它的原因。

    若允许并列两条 `succeeded`，"哪一条是缓存"就没有答案了。
    """
    task_id, attachment_id, _ = _seed_task_with_parse(schema_conn, "GATE-3")
    _seed_parse_row(schema_conn, task_id, attachment_id, status="succeeded", version=2)

    with pytest.raises(sqlite3.IntegrityError):
        _seed_parse_row(
            schema_conn, task_id, attachment_id, status="succeeded", version=3
        )
    schema_conn.rollback()


def test_error_code_retryability_classification() -> None:
    """错误码的可重试性分类必须正确——这是"重试还是立即 blocked"的唯一判据。

    最容易分错的是**存储类**错误：

    - 存储超时 / 连不上 → 重试通常就好 → **可重试**；
    - 路径越界 / 权限不足 / 校验和不符 → 重试一万次也一样 → **不可重试**。

    把前者归为确定性错误，会让一次网络抖动就把任务打成永久失败。
    """
    # 瞬时错误
    assert is_retryable(ErrorCode.APPROVAL_API_TIMEOUT)
    assert is_retryable(ErrorCode.APPROVAL_UNREACHABLE)
    # 限流是 4xx，但必须可重试 —— 它是"请稍后再来"，不是"你的请求有问题"。
    # 漏掉它会让对方一限流我们就直接把任务打成永久失败。
    assert is_retryable(ErrorCode.APPROVAL_RATE_LIMITED)
    assert is_retryable(ErrorCode.STORAGE_UNAVAILABLE)

    # 确定性错误
    assert not is_retryable(ErrorCode.ATTACHMENT_MISSING)
    assert not is_retryable(ErrorCode.STORAGE_PATH_INVALID)
    assert not is_retryable(ErrorCode.STORAGE_WRITE_DENIED)
    assert not is_retryable(ErrorCode.CHECKSUM_MISMATCH)

    # 未知与空值一律按"不可重试"处理：宁可停下让人看，也不做无意义重试
    assert not is_retryable(None)
    assert not is_retryable("NOT_A_REAL_CODE")
