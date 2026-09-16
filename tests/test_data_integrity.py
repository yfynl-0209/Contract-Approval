"""数据完整性与数据库层约束测试。

本文件针对一批**真实存在的漏洞**：

1. **枚举删了、数据库还接受** —— `WriteStatus` 里的 `rejected` 已从 Python 删除，
   但数据库没有任何约束，旧值依然能写入。只靠 Python 枚举校验是不够的：
   脚本、手工 SQL、未来的其他服务都能绕过 ORM。
2. **状态与计数无取值域约束** —— `job_status='whatever'`、`max_attempts=0`、
   `retry_count=-1` 这类值会静默入库，然后让"为什么这条任务卡住了"变得极难排查。
3. **`updated_at` 不会自动更新** —— SQLite 的 `DEFAULT CURRENT_TIMESTAMP`
   只在 **INSERT** 时生效；缺少 `onupdate` 时更新时间永远停在创建时间。
4. **CHECK 约束两侧漂移** —— `db/schema.sql` 是真相来源，但 M9 会用 Alembic
   从 **ORM 元数据**生成 PostgreSQL 迁移。ORM 少了 CHECK，
   迁移出的表会**静默丢失全部取值域约束**。

因此这里不只测"能写入"，而是测"**非法值必须被拒绝**"。
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import CheckConstraint, create_engine, text
from sqlalchemy.orm import Session

import app.models  # noqa: F401  导入以触发全部 ORM 模型注册
from app.db import Base
from app.models import ApprovalTask

# ============================================================
# 局部夹具助手（不依赖其他测试模块，避免跨模块导入的脆弱性）
# ============================================================


def _insert(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    """执行插入并返回新行 id。"""
    conn.execute(sql, params)
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _new_task(conn: sqlite3.Connection, code: str) -> int:
    return _insert(
        conn,
        "INSERT INTO approval_tasks (approval_code, instance_id, context_status) "
        "VALUES (?, ?, 'complete')",
        (code, code),
    )


def _new_attachment(conn: sqlite3.Connection, task_id: int) -> int:
    return _insert(
        conn,
        "INSERT INTO approval_attachments (task_id, attachment_id, file_name) "
        "VALUES (?, 'A-CHK', 'sample.pdf')",
        (task_id,),
    )


def _new_parse(conn: sqlite3.Connection, task_id: int, attachment_id: int) -> int:
    return _insert(
        conn,
        "INSERT INTO contract_parses (task_id, attachment_id, parse_version) "
        "VALUES (?, ?, 1)",
        (task_id, attachment_id),
    )


def _new_run(conn: sqlite3.Connection, task_id: int, parse_id: int) -> int:
    return _insert(
        conn,
        "INSERT INTO review_runs (task_id, parse_id, version_no) "
        "VALUES (?, ?, 1)",
        (task_id, parse_id),
    )


def _new_rule(conn: sqlite3.Connection) -> int:
    return _insert(
        conn,
        "INSERT INTO review_rules (rule_code, rule_name, match_mode, match_text) "
        "VALUES ('CHK-RULE', '约束测试规则', 'keyword', '{}')",
    )


def _new_result(conn: sqlite3.Connection, task_id: int, run_id: int) -> int:
    return _insert(
        conn,
        "INSERT INTO review_results "
        "(task_id, run_id, overall_risk_level, version_no, result_fingerprint) "
        "VALUES (?, ?, 'low', 1, 'fp-integrity')",
        (task_id, run_id),
    )


# ============================================================
# 1. 非法状态必须被**数据库**拒绝
# ============================================================


#: (说明, 直接执行的 SQL)。这些 SQL 的每一行都对应一个曾经能写入的非法值。
INVALID_TASK_AND_JOB_INSERTS: list[tuple[str, str]] = [
    # ---- 被删除的历史枚举取值 ----
    (
        "write_status = 'rejected'（第五个值已从枚举删除）",
        "INSERT INTO approval_tasks (approval_code, instance_id, write_status) "
        "VALUES ('E-01', 'E-01', 'rejected')",
    ),
    (
        "comment 状态同理：write_status 非法时不得入库",
        "INSERT INTO approval_tasks (approval_code, instance_id, write_status) "
        "VALUES ('E-02', 'E-02', 'whatever')",
    ),
    # ---- 去重键组成不得为空 ----
    (
        "instance_id 为空串（会让唯一约束形同虚设）",
        "INSERT INTO approval_tasks (approval_code, instance_id) VALUES ('E-03', '')",
    ),
    (
        "approval_code 为空串",
        "INSERT INTO approval_tasks (approval_code, instance_id) VALUES ('', 'E-04')",
    ),
    (
        "provider 为空串",
        "INSERT INTO approval_tasks (approval_code, instance_id, provider) "
        "VALUES ('E-05', 'E-05', '')",
    ),
    (
        "tenant_id 为纯空白",
        "INSERT INTO approval_tasks (approval_code, instance_id, tenant_id) "
        "VALUES ('E-06', 'E-06', '   ')",
    ),
    # ---- 状态枚举 ----
    (
        "task_status 非法",
        "INSERT INTO approval_tasks (approval_code, instance_id, task_status) "
        "VALUES ('E-07', 'E-07', 'whatever')",
    ),
    (
        "context_status 非法",
        "INSERT INTO approval_tasks (approval_code, instance_id, context_status) "
        "VALUES ('E-08', 'E-08', 'whatever')",
    ),
    (
        "context_source 非法",
        "INSERT INTO approval_tasks (approval_code, instance_id, context_source) "
        "VALUES ('E-09', 'E-09', 'whatever')",
    ),
    (
        "blocked_stage 非法",
        "INSERT INTO approval_tasks (approval_code, instance_id, blocked_stage) "
        "VALUES ('E-10', 'E-10', 'whatever')",
    ),
    # ---- 权威上下文的业务事实取值域 ----
    (
        "our_party_contract_label 非法",
        "INSERT INTO approval_tasks (approval_code, instance_id, our_party_contract_label) "
        "VALUES ('E-11', 'E-11', 'party_c')",
    ),
    (
        "our_party_business_role 非法",
        "INSERT INTO approval_tasks (approval_code, instance_id, our_party_business_role) "
        "VALUES ('E-12', 'E-12', '甲方')",
    ),
    (
        "contract_type 非法",
        "INSERT INTO approval_tasks (approval_code, instance_id, contract_type) "
        "VALUES ('E-13', 'E-13', 'whatever')",
    ),
    # ---- 计数不得为负 ----
    (
        "retry_count 为负",
        "INSERT INTO approval_tasks (approval_code, instance_id, retry_count) "
        "VALUES ('E-14', 'E-14', -1)",
    ),
    # ---- 作业表 ----
    (
        "job_status 非法",
        "INSERT INTO workflow_jobs (job_type, idempotency_key, job_status) "
        "VALUES ('pull', 'K-01', 'whatever')",
    ),
    (
        "job_type 非法",
        "INSERT INTO workflow_jobs (job_type, idempotency_key) "
        "VALUES ('whatever', 'K-02')",
    ),
    (
        "idempotency_key 为空串",
        "INSERT INTO workflow_jobs (job_type, idempotency_key) VALUES ('pull', '')",
    ),
    (
        "max_attempts = 0（瞬时错误将无法重试，任务静默 blocked）",
        "INSERT INTO workflow_jobs (job_type, idempotency_key, max_attempts) "
        "VALUES ('pull', 'K-03', 0)",
    ),
    (
        "attempt_no 为负",
        "INSERT INTO workflow_jobs (job_type, idempotency_key, attempt_no) "
        "VALUES ('pull', 'K-04', -1)",
    ),
    # ---- 日志表 ----
    (
        "log_level 非法",
        "INSERT INTO task_logs (log_level, log_type) VALUES ('whatever', 'pull')",
    ),
    (
        "log_type 为空串",
        "INSERT INTO task_logs (log_level, log_type) VALUES ('info', '')",
    ),
]


@pytest.mark.parametrize(
    ("label", "sql"),
    INVALID_TASK_AND_JOB_INSERTS,
    ids=[item[0] for item in INVALID_TASK_AND_JOB_INSERTS],
)
def test_invalid_value_rejected_by_database(
    schema_conn: sqlite3.Connection, label: str, sql: str
) -> None:
    """非法状态与非法计数必须被**数据库**拒绝，而不是只被 Python 枚举拦下。

    这条测试的名字就是它要证明的事：**约束在库里，不在代码里**。
    只靠 ORM 校验的问题是——脚本、手工 SQL、未来的其他服务都能绕过去，
    而数据库对此毫无察觉。
    """
    with pytest.raises(sqlite3.IntegrityError):
        schema_conn.execute(sql)
    # 必须回滚：schema_conn 是模块级夹具，残留的事务会影响后续用例
    schema_conn.rollback()


def test_attachment_download_status_is_constrained(
    schema_conn: sqlite3.Connection,
) -> None:
    """附件的下载状态同样受约束（此前 'whatever' 可以写入）。"""
    task_id = _new_task(schema_conn, "ATT-CHK")
    attachment_id = _new_attachment(schema_conn, task_id)

    with pytest.raises(sqlite3.IntegrityError):
        schema_conn.execute(
            "UPDATE approval_attachments SET download_status = 'whatever' WHERE id = ?",
            (attachment_id,),
        )
    schema_conn.rollback()


def test_negative_file_size_is_constrained(schema_conn: sqlite3.Connection) -> None:
    """附件大小不得为负——负数会让"文件是否为空"的判断失去意义。"""
    task_id = _new_task(schema_conn, "ATT-SIZE")
    attachment_id = _new_attachment(schema_conn, task_id)

    with pytest.raises(sqlite3.IntegrityError):
        schema_conn.execute(
            "UPDATE approval_attachments SET file_size = -1 WHERE id = ?",
            (attachment_id,),
        )
    schema_conn.rollback()


def test_rule_evaluation_status_is_constrained(
    schema_conn: sqlite3.Connection,
) -> None:
    """规则评价只允许四态——出现第五个值就意味着四态语义被破坏。"""
    task_id = _new_task(schema_conn, "EVAL-CHK")
    attachment_id = _new_attachment(schema_conn, task_id)
    parse_id = _new_parse(schema_conn, task_id, attachment_id)
    run_id = _new_run(schema_conn, task_id, parse_id)
    rule_id = _new_rule(schema_conn)

    with pytest.raises(sqlite3.IntegrityError):
        schema_conn.execute(
            "INSERT INTO rule_hits (run_id, task_id, rule_id, risk_level, hit_status) "
            "VALUES (?, ?, ?, 'high', 'whatever')",
            (run_id, task_id, rule_id),
        )
    schema_conn.rollback()


def test_comment_write_status_rejects_removed_value(
    schema_conn: sqlite3.Connection,
) -> None:
    """数据库也必须拒绝 `rejected`。

    这是本次状态收敛最容易漏掉的一半：Python 枚举删了、文档改了，
    但数据库没有任何约束 —— 约束看起来生效，实际漏风。
    """
    task_id = _new_task(schema_conn, "CMT-CHK")
    attachment_id = _new_attachment(schema_conn, task_id)
    parse_id = _new_parse(schema_conn, task_id, attachment_id)
    run_id = _new_run(schema_conn, task_id, parse_id)
    result_id = _new_result(schema_conn, task_id, run_id)

    with pytest.raises(sqlite3.IntegrityError):
        schema_conn.execute(
            "INSERT INTO comment_logs (task_id, review_id, idempotency_key, write_status) "
            "VALUES (?, ?, 'K-CMT-1', 'rejected')",
            (task_id, result_id),
        )
    schema_conn.rollback()


def test_invalid_rule_match_mode_is_constrained(schema_conn: sqlite3.Connection) -> None:
    """规则的 match_mode 只允许四种——第五种会让评价引擎无从执行。"""
    with pytest.raises(sqlite3.IntegrityError):
        schema_conn.execute(
            "INSERT INTO review_rules (rule_code, rule_name, match_mode, match_text) "
            "VALUES ('CHK-MODE', '非法模式', 'semantic', '{}')"
        )
    schema_conn.rollback()


# ============================================================
# 2. updated_at 必须随更新前进
# ============================================================


def test_updated_at_advances_on_orm_update(work_dir: Path) -> None:
    """ORM 更新必须让 `updated_at` 前进。

    背景：`server_default=func.current_timestamp()` 只在 **INSERT** 时生效。
    缺少 `onupdate` 时 `updated_at` 会永远停在创建时间，而
    "重复拉取只更新已有记录、不新建"这条需求恰恰依赖它体现"刚被刷新过"。

    做法：先把 `updated_at` 手工拨回 2000 年（绕过 ORM，因此不会触发 onupdate），
    再做一次普通 ORM 属性修改，断言它被推进到近期。
    这样断言不受 SQLite `CURRENT_TIMESTAMP` 只有秒级精度的影响。
    """
    db_path = work_dir / "updated_at.db"
    eng = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
    # 用 ORM 元数据建表：顺带证明 CHECK 约束确实挂在 ORM 定义上
    Base.metadata.create_all(eng)

    stale = "2000-01-01 00:00:00"
    try:
        with Session(eng) as session:
            session.add(
                ApprovalTask(
                    approval_code="UPD-1",
                    instance_id="UPD-1",
                    approval_title="初始标题",
                    context_status="complete",
                )
            )
            session.commit()

            # 绕过 ORM 直接改时间：原生 SQL 不会触发 onupdate
            session.execute(
                text(
                    "UPDATE approval_tasks SET updated_at = :ts "
                    "WHERE instance_id = 'UPD-1'"
                ),
                {"ts": stale},
            )
            session.commit()
            session.expire_all()
            assert (
                session.execute(
                    text(
                        "SELECT updated_at FROM approval_tasks "
                        "WHERE instance_id = 'UPD-1'"
                    )
                ).scalar()
                == stale
            ), "前置条件失败：未能把 updated_at 拨回过去"

            # 一次普通 ORM 更新
            task = session.query(ApprovalTask).filter_by(instance_id="UPD-1").one()
            task.approval_title = "改过的标题"
            session.commit()

            after = session.execute(
                text(
                    "SELECT updated_at FROM approval_tasks WHERE instance_id = 'UPD-1'"
                )
            ).scalar()
    finally:
        eng.dispose()

    assert after != stale, (
        "updated_at 没有随 ORM 更新前进：说明缺少 onupdate，"
        "它只在 INSERT 时被写入过"
    )


# ============================================================
# 3. CHECK 约束在 SQL 与 ORM 两侧必须一致
# ============================================================


def _normalize(expr: str) -> str:
    """规范化 CHECK 表达式，便于两侧比对。

    只折叠连续空白、去掉首尾空白；**不改变大小写**——
    若一侧写 `IS NULL`、另一侧写 `is null`，那是真实的不一致，
    应该让它显式失败，而不是被规范化悄悄抹平。
    """
    return " ".join(expr.split())


def _iter_check_exprs(ddl: str) -> Iterator[str]:
    """扫描建表语句，按括号配对取出每个 `CHECK (...)` 的内容。

    两个必须注意的坑：

    1. **不能用简单的子串查找定位关键字**：`file_checksum`、`source_checksum`
       这些列名里就含 "CHECK"。子串查找会把它们误判为约束关键字，
       再去匹配"后面第一个左括号"，于是抓到的是 `UNIQUE (task_id, attachment_id)`
       这种完全无关的内容。因此必须用 `\\bCHECK\\b` 词边界
       （`_` 属于单词字符，所以 `file_checksum` 里的 CHECK 两侧都没有边界，不会命中）。

    2. **不能用正则直接提取括号内容**：CHECK 表达式里有嵌套括号
       （如 `confirmed_digest IS NULL OR content_digest IS NOT NULL` 所在分组、
       各类 `>= 0` 比较），正则会提前截断，把长表达式误判为"不匹配"。
       因此正则只用来定位关键字，括号配对仍用计数器完成。
    """
    for match in re.finditer(r"\bCHECK\b", ddl):
        open_paren = ddl.find("(", match.end())
        if open_paren == -1:
            return
        # 关键字与左括号之间只允许空白；出现其他字符说明这不是 CHECK 约束
        if ddl[match.end() : open_paren].strip():
            continue

        depth = 0
        end = -1
        for index in range(open_paren, len(ddl)):
            char = ddl[index]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    end = index
                    break
        if end == -1:
            return

        yield ddl[open_paren + 1 : end]


def _ddl_checks(conn: sqlite3.Connection, table: str) -> set[str]:
    """取出 `schema.sql` 落到库里的全部 CHECK 表达式（规范化后）。"""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    assert row is not None and row[0], f"表 {table} 在库中不存在"
    return {_normalize(expr) for expr in _iter_check_exprs(row[0])}


def _orm_checks(table_name: str) -> set[str]:
    """取出 ORM 元数据里的全部 CHECK 表达式（规范化后）。"""
    table = Base.metadata.tables[table_name]
    return {
        _normalize(str(constraint.sqltext))
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }


def test_check_constraints_match_between_sql_and_orm(
    schema_conn: sqlite3.Connection,
) -> None:
    """`db/schema.sql` 与 `models.py` 的 CHECK 约束必须**逐条一致（双向包含）**。

    为什么必须守住：表结构的真相来源是 `schema.sql`，
    但 M9 会用 Alembic **从 ORM 元数据**生成 PostgreSQL 迁移。
    ORM 少了 CHECK，迁移出的表会**静默丢失全部取值域约束**——
    建表成功、约束消失，非法状态重新变得可写入。

    单向或双向的缺失都会让这条失败，因此"只在一边加约束"无法蒙混过关。
    """
    problems: list[str] = []

    for table_name in Base.metadata.tables:
        sql_checks = _ddl_checks(schema_conn, table_name)
        orm_checks = _orm_checks(table_name)

        missing_in_orm = sorted(sql_checks - orm_checks)
        missing_in_sql = sorted(orm_checks - sql_checks)

        if missing_in_orm:
            problems.append(f"{table_name}: ORM 缺少 CHECK → {missing_in_orm}")
        if missing_in_sql:
            problems.append(f"{table_name}: SQL 缺少 CHECK → {missing_in_sql}")

    assert not problems, "CHECK 约束两侧不一致：" + "；".join(problems)


def test_every_table_has_at_least_one_check(schema_conn: sqlite3.Connection) -> None:
    """每张表都应至少有一条 CHECK。

    本项目的每张表都有状态、计数或非空键需要约束；
    某张表突然一条都没有，通常意味着有人把约束删掉了。
    """
    without_checks = [
        name
        for name in Base.metadata.tables
        if not _ddl_checks(schema_conn, name)
    ]
    assert not without_checks, f"以下表没有任何 CHECK 约束：{without_checks}"


# ============================================================
# 3.1 部分唯一索引的判定条件（SQL 与 ORM 两侧一致）
# ============================================================


def _ddl_partial_index_where(conn: sqlite3.Connection, index_name: str) -> str | None:
    """取出 schema.sql 建出的部分索引的 WHERE 子句（规范化后）。"""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
        (index_name,),
    ).fetchone()
    assert row is not None and row[0], f"索引 {index_name} 在库中不存在"

    parts = re.split(r"\bWHERE\b", row[0], maxsplit=1)
    return None if len(parts) == 1 else _normalize(parts[1])


def _orm_partial_index_where(table_name: str, index_name: str) -> str | None:
    """取出 ORM 元数据里同名索引的 WHERE 子句（规范化后）。"""
    table = Base.metadata.tables[table_name]
    for index in table.indexes:
        if index.name != index_name:
            continue
        where = index.dialect_options.get("sqlite", {}).get("where")
        return None if where is None else _normalize(str(where))
    raise AssertionError(f"ORM 元数据里没有索引 {index_name}")


def test_partial_index_predicate_matches_between_sql_and_orm(
    schema_conn: sqlite3.Connection,
) -> None:
    """缓存占位闸门的 WHERE 条件必须两侧一致。

    为什么单独守这一条：CHECK 的两侧比对**只覆盖 CHECK**，
    部分索引的判定条件原先没有任何检查。而两侧不一致的后果很具体 ——

    M9 用 Alembic 从 **ORM 元数据**生成 PostgreSQL 迁移。ORM 侧漏写 WHERE，
    迁移出的库上这条闸门就变成了**全局唯一**，于是"失败后可以重新解析"
    在那个环境里**静默失效**，而 SQLite 开发环境一切正常、测试全绿。

    这正是本项目反复出现的一类缺陷：**约束的写法散落各处，
    而它的效果是全库生效**。
    """
    ddl_where = _ddl_partial_index_where(schema_conn, "uq_parse_cache_key")
    orm_where = _orm_partial_index_where("contract_parses", "uq_parse_cache_key")

    assert ddl_where is not None, "缓存闸门必须是**部分**唯一索引，不能是全局唯一"
    assert orm_where is not None, "ORM 侧同样必须带 WHERE 条件"
    assert ddl_where == orm_where, (
        f"部分索引判定条件两侧不一致：SQL={ddl_where!r} / ORM={orm_where!r}"
    )
