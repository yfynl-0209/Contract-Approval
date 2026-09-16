"""PostgreSQL 测试夹具（M9 Task 1）。

## 设计要点

- **默认 URL 指向本地 M9 开发容器**（`docker run postgres:16`，端口 55432），
  可用 `M9_TEST_DATABASE_URL` 覆盖。容器不在时**整组跳过**（skip）——
  SQLite 全量测试不因此受影响；验收（`verify_m9.py`）则要求 PG 必须可达。
- **每条测试独享一套表**：`Base.metadata.drop_all + create_all`——
  直接从 ORM 元数据建表（方言无关），不依赖 SQLite 方言的 `db/schema.sql`。
  与 schema.sql 的结构一致性由既有测试（test_schema_consistency /
  test_data_integrity）+ Task 2 的迁移比对共同守住。
- 夹具**不 fake 时间**：租约、退避都用真实 UTC 时间，与生产行为一致。
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base

#: 测试库。默认指向本地 M9 开发容器（见 docs/superpowers/plans/…-M9-…-plan.md Task 1）
TEST_DATABASE_URL = os.environ.get(
    "M9_TEST_DATABASE_URL",
    "postgresql+psycopg://m9:m9@127.0.0.1:55432/m9",
)


@pytest.fixture(scope="session")
def pg_available() -> bool:
    """PG 可达性只探测一次；不可达时整组跳过（SQLite 测试不受影响）。"""
    try:
        probe = create_engine(TEST_DATABASE_URL, pool_pre_ping=True)
        with probe.connect() as conn:
            conn.execute(text("SELECT 1"))
        probe.dispose()
        return True
    except Exception:  # noqa: BLE001 - 探测失败=环境没有 PG，跳过而非报错
        return False


@pytest.fixture()
def pg_engine(pg_available: bool) -> Iterator[Engine]:
    """每条测试独享一套表：从 ORM 元数据 drop_all + create_all。"""
    if not pg_available:
        pytest.skip("PostgreSQL 不可达（M9 开发容器未启动？端口 55432）")
    engine = create_engine(TEST_DATABASE_URL, pool_pre_ping=True, future=True)
    try:
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        yield engine
    finally:
        engine.dispose()


@pytest.fixture()
def pg_sessionmaker(pg_engine: Engine) -> sessionmaker[Session]:
    """与生产 `SessionLocal` 同参的会话工厂（autoflush=False / 显式事务）。"""
    return sessionmaker(bind=pg_engine, autoflush=False, autocommit=False, future=True)


@pytest.fixture()
def pg_session(pg_sessionmaker: sessionmaker[Session]) -> Iterator[Session]:
    session = pg_sessionmaker()
    try:
        yield session
    finally:
        session.close()
