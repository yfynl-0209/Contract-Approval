"""Alembic 迁移测试（M9 Task 2）。

两条线，都在**真实的空 PostgreSQL 库**上跑：

1. **往返**：`upgrade head → 插入数据 → downgrade base → 库空 → 再 upgrade`。
   这是计划要求的 round-trip；"再 upgrade 还能成功"排除了
   "downgrade 把状态留在中间"这一类坏迁移。
2. **结构比对**：upgrade 后的 `information_schema` / `pg_catalog`
   与 ORM 元数据逐表核对 —— 列、主键、外键对、CHECK 数量、
   部分唯一索引的 WHERE 谓词。autogenerate 的产物再漂亮，
   也得有这份比对才能说"迁移出的库不会静默丢约束"。

⚠️ 每条测试独享一个临时数据库（CREATE DATABASE / DROP DATABASE），
互不污染，也不碰 `m9` 默认库。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from alembic import command
from alembic.config import Config

from app.config import PROJECT_ROOT
from tests.postgres.conftest import TEST_DATABASE_URL

ADMIN_URL = TEST_DATABASE_URL  # 用户 m9 有建库权（容器内超户）
ALEMBIC_DIR = str(PROJECT_ROOT / "alembic")


def _make_alembic_config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", ALEMBIC_DIR)
    # env.py 从这里取 URL（优先级最高），而不是写进 ini
    cfg.attributes["url"] = url
    return cfg


@pytest.fixture()
def temp_db_url() -> Iterator[str]:
    """每条测试一个空数据库，跑完即删。"""
    dbname = f"m9_mig_{uuid.uuid4().hex[:8]}"
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{dbname}"'))
    admin.dispose()

    # 拼接 URL：替换库路径（jdbc 风格的最后一段）
    base, _, _ = TEST_DATABASE_URL.rpartition("/")
    yield f"{base}/{dbname}"

    cleanup = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with cleanup.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)'))
    cleanup.dispose()


def _user_tables(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = 'public'"
            )
        )
        return {row[0] for row in rows}


# ============================================================
# 往返：upgrade → downgrade → upgrade
# ============================================================


def test_migration_round_trip(temp_db_url: str) -> None:
    cfg = _make_alembic_config(temp_db_url)
    engine = create_engine(temp_db_url)

    try:
        # 空库 → head
        command.upgrade(cfg, "head")
        tables = _user_tables(engine)
        assert "alembic_version" in tables
        assert "approval_tasks" in tables and "rule_hits" in tables
        assert len(tables) == 14  # 13 张业务表 + alembic_version

        # 插一行真实数据（证明迁移出的库**能用**，不是只有壳）
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO approval_tasks "
                    "(provider, tenant_id, instance_id, approval_code, task_status, "
                    "write_status, context_source, context_status, retry_count) "
                    "VALUES ('mock', 'default', 'HT-MIG-1', 'HT-MIG-1', "
                    "'pending', 'not_written', 'approval_system', 'missing', 0)"
                )
            )

        # head → base：全部业务表消失（alembic_version 保留）
        command.downgrade(cfg, "base")
        tables = _user_tables(engine)
        assert tables == {"alembic_version"}, f"downgrade 后应只剩版本表，实际 {tables}"

        # 再 upgrade：幂等且成功
        command.upgrade(cfg, "head")
        assert len(_user_tables(engine)) == 14
    finally:
        engine.dispose()


# ============================================================
# 结构比对：ORM 元数据 vs PostgreSQL 实际结构
# ============================================================


def _orm_metadata() -> dict[str, dict]:
    """从 ORM 元数据提取比对所需的表结构（列名 / 主键 / 外键对 / CHECK 数）。"""
    from app.db import Base

    tables: dict[str, dict] = {}
    for name, table in Base.metadata.tables.items():
        foreign_keys: dict[tuple[str, ...], str] = {}
        for constraint in table.constraints:
            if constraint.__class__.__name__ == "ForeignKeyConstraint":
                src = tuple(sorted(c.name for c in constraint.columns))
                foreign_keys[src] = constraint.referred_table.name
        tables[name] = {
            "columns": {c.name for c in table.columns},
            "primary_key": {c.name for c in table.primary_key.columns},
            "foreign_keys": foreign_keys,
            "check_count": len(
                [c for c in table.constraints if c.__class__.__name__ == "CheckConstraint"]
            ),
        }
    return tables


def test_migration_structural_parity_with_orm_metadata(temp_db_url: str) -> None:
    """upgrade head 后：列集合 / 主键 / 外键 / CHECK 数量与 ORM 元数据一致。"""
    cfg = _make_alembic_config(temp_db_url)
    command.upgrade(cfg, "head")
    engine = create_engine(temp_db_url)
    expected = _orm_metadata()

    try:
        with engine.connect() as conn:
            # ---- 列集合 ----
            actual_columns: dict[str, set[str]] = {}
            for table, cols, _ in conn.execute(
                text(
                    "SELECT table_name, column_name, ordinal_position "
                    "FROM information_schema.columns WHERE table_schema = 'public'"
                )
            ):
                actual_columns.setdefault(table, set()).add(cols)

            for table, meta in expected.items():
                assert set(meta["columns"]) <= actual_columns.get(table, set()), (
                    f"表 {table} 缺列：{meta['columns'] - actual_columns.get(table, set())}"
                )

            # ---- 主键 ----
            pk_rows = conn.execute(
                text(
                    "SELECT tc.table_name, kcu.column_name "
                    "FROM information_schema.table_constraints tc "
                    "JOIN information_schema.key_column_usage kcu "
                    "ON tc.constraint_name = kcu.constraint_name "
                    "WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = 'public'"
                )
            ).all()
            actual_pk: dict[str, set[str]] = {}
            for table, column in pk_rows:
                actual_pk.setdefault(table, set()).add(column)
            for table, meta in expected.items():
                assert actual_pk.get(table, set()) == meta["primary_key"], (
                    f"表 {table} 主键不符：{actual_pk.get(table)} != {meta['primary_key']}"
                )

            # ---- 外键（来源列对 → 目标表）----
            fk_rows = conn.execute(
                text(
                    "SELECT tc.table_name, kcu.column_name, ccu.table_name "
                    "FROM information_schema.table_constraints tc "
                    "JOIN information_schema.key_column_usage kcu "
                    "ON tc.constraint_name = kcu.constraint_name "
                    "JOIN information_schema.constraint_column_usage ccu "
                    "ON tc.constraint_name = ccu.constraint_name "
                    "WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = 'public'"
                )
            ).all()
            actual_fk: dict[str, set[tuple[str, str]]] = {}
            for table, column, ref_table in fk_rows:
                actual_fk.setdefault(table, set()).add((column, ref_table))
            for table, meta in expected.items():
                for src_pair, ref_table in meta["foreign_keys"].items():
                    for column in src_pair:
                        assert (column, ref_table) in actual_fk.get(table, set()), (
                            f"表 {table} 缺外键 {src_pair} -> {ref_table}"
                            f"（实际：{sorted(actual_fk.get(table, set()))}）"
                        )

            # ---- CHECK 数量（迁移出的库不得静默丢 CHECK）----
            check_rows = conn.execute(
                text(
                    "SELECT conrelid::regclass::text, count(*) FROM pg_constraint "
                    "WHERE contype = 'c' AND connamespace = 'public'::regnamespace "
                    "GROUP BY conrelid::regclass::text"
                )
            ).all()
            actual_checks = dict(check_rows)
            for table, meta in expected.items():
                expected_count = meta["check_count"]
                if expected_count:
                    assert actual_checks.get(table, 0) >= expected_count, (
                        f"表 {table} CHECK 数不足：{actual_checks.get(table, 0)} < {expected_count}"
                        "（迁移静默丢约束是最难查的一类回归）"
                    )
    finally:
        engine.dispose()


def test_migration_partial_unique_index_keeps_predicate(temp_db_url: str) -> None:
    """解析缓存闸门在 PG 下必须仍是**部分**索引 —— 全局唯一 = 失败后无法重新解析。

    这是对"迁移地雷"（部分唯一索引在换库后退化为全局唯一）的钉子。
    """
    cfg = _make_alembic_config(temp_db_url)
    command.upgrade(cfg, "head")
    engine = create_engine(temp_db_url)

    try:
        with engine.connect() as conn:
            indexdef = conn.execute(
                text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE indexname = 'uq_parse_cache_key'"
                )
            ).scalar()
        assert indexdef is not None, "uq_parse_cache_key 索引不存在"
        assert "WHERE" in indexdef.upper(), (
            f"部分唯一索引丢掉了谓词（退化成全局唯一）：{indexdef}"
        )
        assert "parse_status" in indexdef
    finally:
        engine.dispose()
